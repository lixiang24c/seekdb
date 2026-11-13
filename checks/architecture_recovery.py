#!/usr/bin/env python3
"""Kill/error at native sync and Python manifest boundaries, with actual restart."""
import argparse,json,os,signal,stat,subprocess,sys,tempfile,threading,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session
WRAPPER=r'''
import os,signal,sys
root=sys.argv[1];original=os.replace
from minidb.native_storage import NativeStorage
assert NativeStorage().available(), 'native storage must be loaded'
def replace(src,dst):
    armed=os.path.exists(root+'/armed') and str(dst).endswith('/manifest.json')
    def stop():
        with open(root+'/fault.reached','w') as marker:marker.write(os.environ['MV_TEST_FAULT'])
        if os.environ.get('MV_TEST_EXTERNAL_KILL')=='1':
            while True:signal.pause()
        os.kill(os.getpid(),signal.SIGKILL)
    if armed and os.environ['MV_TEST_FAULT']=='before':stop()
    original(src,dst)
    if armed and os.environ['MV_TEST_FAULT']=='after':stop()
os.replace=replace
from minidb.server import main
raise SystemExit(main())
'''

def kill_at_marker(marker, fault, send_kill, stop, evidence):
    """Unmeasured observer: kill only after the precise fault marker is visible."""
    try:
        deadline=time.monotonic()+60
        while not stop.wait(0.01):
            if marker.read_text()==fault:
                evidence.update(marker=fault,delivery=send_kill(),status='PASS')
                return
            if time.monotonic()>=deadline:raise TimeoutError('fault marker not reached')
        evidence.update(status='CANCELLED')
    except Exception as error:evidence.update(status='ERROR',error=str(error))

def main():
 p=argparse.ArgumentParser();p.add_argument('--image');p.add_argument('--memory',default='2g');p.add_argument('--shim',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
 cfg=json.loads((ROOT/'protocol_check/config.json').read_text());cfg.update(startup_timeout=60,query_timeout=60,case_timeout=300)
 results=[]
 external=bool(a.image) or os.environ.get('MV_TEST_EXTERNAL_KILL')=='1'
 report={'status':'RUNNING','memory':a.memory if a.image else 'host-unlimited','phase':'startup_preflight','preflight':None,'cases':results,'directories':[]}
 def save(): (a.output/'report.json').write_text(json.dumps(report,indent=2))
 save()
 for cut in ('kill','eio','before','after'):
  for explicit in (False,True):
   with tempfile.TemporaryDirectory(prefix='architecture-recovery-') as directory:
    root=Path(directory);data='/data' if a.image else directory;container='architecture-fault-'+uuid.uuid4().hex
    original_mode=stat.S_IMODE(root.stat().st_mode)
    # Only this freshly created, synthetic-data directory is shared with the
    # capability-free container. Keep the container's UID and isolation intact.
    if a.image:root.chmod(0o777)
    # The shim opens this with mode 0600. Pre-create a host-owned writable marker
    # so O_TRUNC preserves its mode and the host can read the observed fault.
    marker=root/'fault.reached';marker.touch()
    if a.image:marker.chmod(0o666)
    report['directories'].append({'path':directory,'uid':root.stat().st_uid,'gid':root.stat().st_gid,
      'original_mode':oct(original_mode),'mode':oct(stat.S_IMODE(root.stat().st_mode)),
      'marker_mode':oct(stat.S_IMODE(marker.stat().st_mode)),'mount':'bind,rw -> /data'})
    save()
    def close_client(s):
     # A timed-out fault observer can leave PID 1 waiting. Remove only this
     # fixture's container; killing its Docker CLI alone would leave it alive.
     if a.image and s.proc is not None and s.proc.poll() is None:
      subprocess.run(['docker','rm','--force',container],capture_output=True,timeout=20)
     s.close()
    def client(fault):
     argv=[sys.executable,'-B','-u','-c',WRAPPER,data] if fault else [sys.executable,'-B','-u','-m','minidb.server',data]
     if a.image:
      argv=['docker','run','--rm','--name',container,'-i','--network=none','--cpus=2','--memory='+a.memory,'--memory-swap='+a.memory,'--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128','-v',directory+':/data','-v',str(a.shim.resolve())+':/fault.so:ro','-e','LD_PRELOAD=/fault.so','-e','MV_TEST_ROOT=/data','-e','MV_TEST_FAULT='+fault,'-e','MV_TEST_EXTERNAL_KILL=1','--entrypoint=python3',a.image,*argv[1:]]
     else:
      argv=['env','LD_PRELOAD='+str(a.shim.resolve()),'MV_TEST_ROOT='+data,'MV_TEST_FAULT='+fault,'MV_TEST_EXTERNAL_KILL='+str(int(external)),*argv]
     s=Session(cfg,a.output/(cut+str(explicit)+'.stderr.log'),command=argv,case_timeout=300)
     try:s.start()
     except Exception as error:
      report.update(status='BLOCKED_STARTUP',error=str(error));save();close_client(s);raise
     return s
    def q(s,text):
     r=s.request(text);assert r['ok'],r;return r
    if report['preflight'] is None:
     s=client('')  # No fault mode and no armed marker during the startup check.
     try:
      isolation=None
      if a.image:
       inspected=json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
       (a.output/'preflight.inspect.json').write_text(json.dumps(inspected,indent=2))
       isolation=inspected['HostConfig'];limit=int(a.memory[:-1])*(1<<30 if a.memory.endswith('g') else 1<<20)
       assert isolation['NetworkMode']=='none' and isolation['ReadonlyRootfs']
       assert not isolation['Privileged'] and 'ALL' in isolation['CapDrop']
       assert not isolation.get('CapAdd') and any(opt in ('no-new-privileges','no-new-privileges=true') for opt in isolation['SecurityOpt'])
       assert isolation['Memory']==isolation['MemorySwap']==limit and isolation['NanoCpus']==2000000000
       assert isolation['PidsLimit']==128
       mount=next(m for m in inspected['Mounts'] if m['Destination']=='/data')
       assert mount['Type']=='bind' and mount['Source']==directory and mount['RW']
      q(s,'CREATE TABLE permission_probe(id BIGINT PRIMARY KEY)')
      q(s,'INSERT INTO permission_probe VALUES (7)')
      assert q(s,'SELECT id FROM permission_probe')['rows']==[[7]]
      assert 'permission_probe' in json.loads((root/'manifest.json').read_text())['tables']
      s.shutdown()
      report['preflight']={'status':'PASS','ready':True,'database_read_write':True,'isolation_checked':bool(a.image)}
      report['phase']='fault_injection';save();print(json.dumps({'preflight':report['preflight']}),flush=True)
     except Exception as error:
      report.update(status='BLOCKED_PREFLIGHT',error=str(error));save();raise
     finally:close_client(s)
    s=client(cut);watcher=None;stop=threading.Event();delivery={}
    try:
     q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(16))')
     q(s,'CREATE VECTOR INDEX ix ON t(v)')
     vec=json.dumps([1.]*16)
     for start in range(0,700,100):q(s,'INSERT INTO t VALUES '+','.join(f"({i},0,'{vec}')" for i in range(start,start+100)))
     if explicit:q(s,'BEGIN')
     (root/'armed').touch()
     stmt='INSERT INTO t VALUES '+','.join(f"({i},1,'{vec}')" for i in range(700,800))
     if explicit and cut in ('before','after'):
      q(s,stmt);stmt='COMMIT'
     if external and cut!='eio':
      def send_kill():
       if a.image:
        command=['docker','kill','--signal=KILL',container]
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        if result.returncode:raise RuntimeError('docker kill failed: '+result.stderr)
        return {'command':command,'exit_code':result.returncode,'stdout':result.stdout.strip()}
       os.kill(s.proc.pid,signal.SIGKILL)
       return {'sender':'host test process','pid':s.proc.pid,'signal':signal.SIGKILL}
      watcher=threading.Thread(target=kill_at_marker,args=(marker,cut,send_kill,stop,delivery),daemon=True)
      watcher.start()
     try:s.request(stmt)
     except Exception:pass
     else:raise AssertionError('fault was not reached')
     code=s.proc.wait(timeout=30)
     if watcher:
      watcher.join(timeout=25)
      if delivery.get('status')!='PASS':
       report.update(status='BLOCKED_FAULT_INJECTION',error='External kill not confirmed',delivery=delivery);save()
       raise AssertionError(delivery)
     assert code==(1 if cut=='eio' else 137 if a.image else -9),(cut,code)
     assert (root/'fault.reached').read_text()==cut,'Must reach the injected fault; OOM is not a successful kill test'
    except Exception as error:
     if report['status']=='RUNNING':report.update(status='FAILED_FAULT_CHECK',error=str(error))
     report['failed_case']={'fault':cut,'transaction':explicit,'delivery':delivery};save();raise
    finally:
     stop.set()
     if watcher:watcher.join(timeout=25)
     close_client(s)
    (root/'armed').unlink()
    if cut=='eio':assert 'sync segment' in (a.output/(cut+str(explicit)+'.stderr.log')).read_text()
    for _ in range(2):
     s=client('')
     try:
      n=800 if cut=='after' else 700
      assert q(s,'SELECT id,c FROM t ORDER BY id')['rows']==[[i,int(i>=700)] for i in range(n)]
      assert q(s,"SELECT id FROM t ORDER BY L2_DISTANCE(v,'"+vec+"') LIMIT 10")['rows']==[[i] for i in range(10)]
      m=json.loads((root/'manifest.json').read_text());keep={v[k] for t in m['tables'].values() for v in t['segments'] for k in ('file','keys','index_file') if v.get(k)}
      assert {f.name for f in root.glob('mv-*')}==keep
      s.shutdown()
     finally:close_client(s)
    results.append({'fault':cut,'transaction':explicit,'status':'PASS','two_restarts':True,'exit_code':code,'external_kill':delivery or None})
    save()
    print(json.dumps(results[-1]),flush=True)
 report.update(status='PASS',phase='complete');save()
if __name__=='__main__':main()
