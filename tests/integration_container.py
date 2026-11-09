"""Real Docker lifecycle test for the current local SQL adapter."""
import json,os,select,subprocess,tempfile,uuid

def run(image):
 with tempfile.TemporaryDirectory(prefix='minivector-container-') as data:
  processes=[];names=[]
  def start():
   name='minivector-test-'+uuid.uuid4().hex;names.append(name)
   p=subprocess.Popen(['docker','run','--rm','--name',name,'--network','none','--cpus','2','--memory','128m','--memory-swap','128m','-i','-v',data+':/data',image],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
   processes.append(p)
   ready=p.stdout.readline().strip()
   assert ready=='LOCAL_SQL_READY',(ready,p.stderr.read() if p.poll() is not None else '')
   return p,name
  def execute(p,sql):
   p.stdin.write(sql+';\n');p.stdin.flush();rows=[]
   while True:
    line=p.stdout.readline()
    assert line,'container exited unexpectedly'
    value=json.loads(line)
    if 'row' in value:rows.append(value['row'])
    if 'ok' in value:
     assert value['ok'],value
     return rows
  try:
   p,name=start()
   execute(p,'CREATE TABLE t(id BIGINT PRIMARY KEY, label VARCHAR(4), v VECTOR(2))')
   execute(p,"INSERT INTO t VALUES (1,'中文',[1,0]),(2,'中文',[-1,0])")
   execute(p,'CREATE VECTOR INDEX ix ON t(v)')
   assert execute(p,"SELECT id FROM t WHERE label='中文' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10")==[[1],[2]]
   execute(p,'BEGIN');execute(p,'UPDATE t SET v=[0,0] WHERE id=2');execute(p,'COMMIT')
   execute(p,'BEGIN');execute(p,'DELETE FROM t WHERE id=1')
   subprocess.run(['docker','kill','--signal','KILL',name],check=True,capture_output=True);p.wait(timeout=10)
   p,name=start()
   assert execute(p,'SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10')==[[2],[1]]
   execute(p,'BEGIN');execute(p,"INSERT INTO t VALUES (3,'ok',[0,0])")
   p.stdin.close();assert p.wait(timeout=10)==0
   p,name=start()
   assert execute(p,'SELECT id FROM t ORDER BY id')==[[1],[2]]
   p.stdin.close();assert p.wait(timeout=10)==0
   print('PASS: Docker build/runtime, offline network, 2 CPUs/128MiB, SQL/index, confirmed commit, SIGKILL recovery, EOF rollback/restart')
  finally:
   for name in names:subprocess.run(['docker','rm','-f',name],capture_output=True)
   for p in processes:
    if p.poll() is None:p.kill();p.wait()
    for stream in (p.stdin,p.stdout,p.stderr):
     if stream and not stream.closed:stream.close()
if __name__=='__main__':run(os.environ.get('MINIVECTOR_IMAGE','minivectordb:round2-local'))
