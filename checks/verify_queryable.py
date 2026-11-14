#!/usr/bin/env python3
"""Freeze/build both engines offline; gate 21 same-host workloads on correctness.

Does not change the default engine or any existing database. No commit/push.
Main 1c384m: seeds 42/43/44, baseline/final/three single-switch ablations = 15.
Supplementary 1c2g, 2c2g, 1c16m: one baseline/final pair each = 6.
"""
import argparse
import datetime
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import traceback

REPO=Path(__file__).resolve().parents[1]
BASE='122b57649bd50c8d25226bafd1dea0c1c2884e99'
BUILDER='gcc:12-bookworm@sha256:44482187aa3865ad1e2477dc8c56bae4fd8e071e7d99c5f44956effc7f7cc846'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def files():
    paths=[REPO/x for x in ('Dockerfile','.dockerignore','requirements.lock')]
    for folder in ('native','minidb','checks','protocol_check','mysqltest'):
        paths.extend(p for p in (REPO/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.o','.so','.pyc'))
    return {str(p.relative_to(REPO)):digest(p) for p in sorted(paths)}
def percentile(values,p):return sorted(values)[max(0,math.ceil(p*len(values))-1)]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);p.add_argument('--resume',type=Path);p.add_argument('--plan',action='store_true');args=p.parse_args()
    if args.plan:
        print(__doc__);print('Safety gates: recovery at 16m/384m/2g; large data and long transactions at 16m/64m; v1/v2 fixtures; private/host namespace + 6 disabled paths; final-image public/contract/numeric/model.');return 0
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=(args.resume.parent if args.resume else args.output or REPO.parent/'reports'/('queryable-docker-'+stamp)).resolve()
    if args.resume:r=json.loads(args.resume.read_text())
    else:
        out.mkdir(parents=True,exist_ok=False)
        r={'status':'STARTED','baseline':BASE,'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
           'sha256':files(),'conversation_sha256':digest(REPO/'agent/conversation.json'),'images':{},'checks':{},'runs':[],
           'limits':['Same host only. Seeds and shape match; original document generator unavailable, repository random generator used with bare vector syntax.',
                     'Full time includes startup/schema/write/index/first queries/warmup/1000 main queries; shutdown and restart reported separately.',
                     'I/O spans startup through final settled writeback; missing device counters or peak remain null.',
                     'First query is process-cold only; no claim of globally cold page cache.',
                     'Switch comparisons use the same candidate image. Architecture-wide changes are measured together versus exact 122b, not multiplied stage gains.',
                     'Resource layout fixtures test v1/v2; actual cgroup version/namespace availability is reported, not simulated as a second server.',
                     'Supplementary resource comparisons are single-pair observations, not stable gains.'],
           'default_engine_changed':False}
    def persist():save(out/'report.json',r)
    def run(name,argv,cwd=None,timeout=7200):
        if r['checks'].get(name,{}).get('status')=='PASS':return
        words=list(map(str,argv))
        if '--output' in words:
            old=Path(words[words.index('--output')+1])
            if old.exists():
                assert old.parent==out;history=out/'retry-history';history.mkdir(exist_ok=True);old.rename(history/(name+'-'+stamp))
        log=out/(name+'.log')
        if log.exists():shutil.copyfile(log,out/(name+'-'+stamp+'.previous.log'))
        r['active']=name;persist();save(out/(name+'.command.json'),words);print('CHECK',name,flush=True)
        with log.open('wb') as f:result=subprocess.run(words,cwd=cwd or REPO,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
        r['checks'][name]={'status':'PASS' if result.returncode==0 else 'FAIL','returncode':result.returncode,'log':log.name};persist()
        if result.returncode:raise RuntimeError(name+': '+log.read_text(errors='replace')[-3000:])
    try:
        assert files()==r['sha256'],'sources changed; start a new report'
        assert r['baseline']==BASE
        frozen=out/'candidate';baseline=out/'baseline'
        if not frozen.exists():
            frozen.mkdir();baseline.mkdir()
            for name in r['sha256']:
                dest=frozen/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,dest)
            data=subprocess.check_output(['git','archive',BASE,'Dockerfile','.dockerignore','requirements.lock','native','minidb'],cwd=REPO)
            (out/'122b-runtime.tar').write_bytes(data)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:archive.extractall(baseline,filter='data')
        assert all(digest(frozen/n)==h for n,h in r['sha256'].items()),'frozen source changed'
        baseline_hashes={str(p.relative_to(baseline)):digest(p) for p in baseline.rglob('*') if p.is_file()}
        if 'baseline_sha256' in r:assert r['baseline_sha256']==baseline_hashes
        r['baseline_sha256']=baseline_hashes
        for item in json.loads((frozen/'native/vendor/manifest.json').read_text()):assert digest(frozen/'native/vendor'/item['path'])==item['sha256']
        run('docker-version',['docker','version'],timeout=60)
        for label,root,dockerfile in [('baseline',baseline,'Dockerfile'),('candidate',frozen,'native/queryable_log/Dockerfile')]:
            run(label+'-build',['docker','build','--platform=linux/amd64','--no-cache','--network=none','-f',dockerfile,'--iidfile',out/(label+'.id'),'-t','minivectordb:queryable-'+label+'-'+out.name.lower(),'.'],root)
            r['images'][label]=(out/(label+'.id')).read_text().strip()
            run(label+'-inspect',['docker','image','inspect',r['images'][label]],timeout=60)
        image=r['images']['candidate'];assert json.loads((out/'candidate-inspect.log').read_text())[0]['Config']['Entrypoint']==['/app/minivectordb-log']
        fault=out/'test-tools';fault.mkdir(exist_ok=True)
        build=['docker','run','--rm','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
               '--tmpfs','/tmp:rw,nosuid,size=128m','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={frozen},dst=/src,readonly','--mount',f'type=bind,src={fault},dst=/out',BUILDER]
        run('fault-build',build+['gcc','-shared','-fPIC','-O2','/src/checks/queryable_fault.c','-ldl','-o','/out/fault.so'])
        run('units-build',build+['g++','-std=c++17','-O2','-static-libstdc++','-static-libgcc','/src/checks/queryable_units.cpp','-o','/out/units'])
        run('quant-build',build+['gcc','-std=c11','-O3','-ffp-contract=off','-fno-fast-math','/src/checks/queryable_quant.c','-lm','-o','/out/quant'])
        run('allocation-build',build+['bash','-c',
            'set -e\ngcc -O2 -c /src/native/screen.c -ffp-contract=off -fno-fast-math -o /tmp/screen.o\n'
            'gcc -O2 -c /src/native/vendor/yyjson/yyjson.c -o /tmp/json.o\n'
            'gcc -O2 -DUTF8PROC_STATIC -c /src/native/vendor/utf8proc/utf8proc.c -o /tmp/unicode.o\n'
            'g++ -std=c++17 -O2 -static-libstdc++ -static-libgcc -ffp-contract=off -fno-fast-math /src/checks/queryable_allocation.cpp /tmp/screen.o /tmp/json.o /tmp/unicode.o -lm -lz -o /out/allocation'])
        for name in ('units','quant'):
            run(name,['docker','run','--rm','--network=none','--cpus=1','--memory=384m','--memory-swap=384m','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=bind,src={fault},dst=/tests,readonly','--entrypoint','/tests/'+name,image])
        allocation_data=out/('allocation-data-'+stamp);allocation_data.mkdir(exist_ok=True);os.chmod(allocation_data,0o777)
        run('allocation',['docker','run','--rm','--network=none','--cpus=1','--memory=384m','--memory-swap=384m','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=bind,src={fault},dst=/tests,readonly','--mount',f'type=bind,src={allocation_data},dst=/data','--entrypoint','/tests/allocation',image,'/data'])
        gate=[sys.executable,'-B',frozen/'checks/queryable_regression.py','--image',image,'--fault-library',fault/'fault.so']
        # Recovery and reclamation precede general correctness and every benchmark.
        for memory in ('16m','384m','2g'):
            label='recovery-'+memory;run(label,gate+['--section','faults','--memory',memory,'--output',out/label],frozen)
        for memory in ('16m','64m'):
            for section in ('transactions','large'):
                label=section+'-'+memory;run(label,gate+['--section',section,'--memory',memory,'--output',out/label],frozen)
        for memory in ('16m','384m'):
            for ns in ('private','host'):
                label='paths-'+memory+'-'+ns;run(label,gate+['--section','paths','--memory',memory,'--cgroupns',ns,'--output',out/label],frozen)
                detail=json.loads((out/label/'report.json').read_text())['checks']['paths']['diagnostics']
                assert detail['budget_bytes']==(16 if memory=='16m' else 384)*(1<<20)*7//8,detail
                if memory=='384m' and detail['simd']:
                    assert detail['quant_builds']>0 and detail['quant_scan_calls']>0,('quant path not demonstrated',detail)
        for env in ('MINIDB_QUANT_SCREEN=0','MINIDB_COMPACT_VECTOR=0','MINIDB_POSTINGS=0','MINIDB_INDEX_MMAP=0','MINIDB_SIMD=0','MINIDB_FLOAT_SCREEN=0'):
            label=env.split('=')[0].lower();run(label,gate+['--section','paths','--env',env,'--output',out/label],frozen)
        for label,env in [('adversarial',[]),('adversarial-scalar',['--env','MINIDB_SIMD=0'])]:
            run(label,gate+['--section','adversarial','--output',out/label]+env,frozen)
        for cpu,memory in [('1','384m'),('1','16m'),('1','2g'),('2','2g')]:
            config=json.loads((frozen/'protocol_check/config.json').read_text());config.update(cpus=cpu,memory=memory)
            label='public-'+cpu+'c-'+memory;save(out/(label+'.config.json'),config)
            run(label,[sys.executable,'-B',frozen/'protocol_check/run.py','--image',image,'--config',out/(label+'.config.json'),'--output',out/label],frozen)
        run('contract',[sys.executable,'-B',frozen/'checks/contract.py','--image',image,'--output',out/'contract'],frozen)
        for section,extra,label in [('all',[],'native-all'),('topk',['--scalar'],'native-scalar')]:
            argv=[sys.executable,'-B',frozen/'checks/native_validation.py','--image',image,'--section',section,'--output',out/label]+extra
            run(label,argv,frozen)
        run('baseline-public',[sys.executable,'-B',frozen/'protocol_check/run.py','--image',r['images']['baseline'],'--output',out/'baseline-public'],frozen)
        sys.path.insert(0,str(frozen/'protocol_check'));sys.path.insert(0,str(frozen/'checks'))
        from _transport import Session,ProtocolError
        import optimization_resources as resources
        import optimization_workload as work
        # Keep the observer outside the measured cgroup. Optional counters stay null.
        observer_code=out/'observer';observer_code.mkdir(exist_ok=True)
        shutil.copyfile(frozen/'checks/queryable_observer.py',observer_code/'optimization_observer.py')
        resources.OUT=out;resources.HERE=observer_code;resources.IMAGE=image
        class Observer(resources.Observer):
            def snapshot(self,drain=False):
                self.p.stdin.write((json.dumps({'op':'snapshot','drain':drain})+'\n').encode());self.p.stdin.flush();x=self.read();assert x.get('ok'),x;return x['snapshot']
        class VariantSession(Session):
            def __init__(self,*a,extra_env=(),**kw):super().__init__(*a,**kw);self.extra_env=extra_env
            def start(self,ready=True):
                if not self.extra_env:return super().start(ready)
                if not self.volume_created:self.docker('volume','create',self.volume);self.volume_created=True
                image=self.image
                self.command=['docker','run','--name',self.name,'-i','--network=none','--cpus='+self.config['cpus'],'--memory='+self.config['memory'],'--memory-swap='+self.config['memory'],'--pids-limit=128','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--mount',f'type=volume,src={self.volume},dst=/data']
                for item in self.extra_env:self.command+=['-e',item]
                self.command.append(image);self.image=None
                try:return super().start(ready)
                finally:self.image=image
        # Preserve a real old-engine fixture, and test refusal on its own copy.
        if r['checks'].get('old-data-boundary',{}).get('status')!='PASS':
            old_root=out/'old-engine-data';old_root.mkdir(exist_ok=True);config=json.loads((frozen/'protocol_check/config.json').read_text())
            old=Session(config,out/'old-data.stderr',image=r['images']['baseline'],data_root=str(old_root));os.chmod(old.bind_data.name,0o777)
            try:
                old.start();work.request(old,'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))');work.request(old,'INSERT INTO t VALUES (1,[3,4])');old.shutdown()
                retained=out/('retained-old-data-'+stamp);shutil.copytree(old.bind_data.name,retained)
                probe=out/('old-data-probe-'+stamp);shutil.copytree(retained,probe);before={p.name:digest(p) for p in probe.iterdir() if p.is_file()}
                refusal=subprocess.run(['docker','run','--rm','--network=none','--cpus=1','--memory=384m','--memory-swap=384m','--cap-drop=ALL','--security-opt=no-new-privileges','--read-only','--mount',f'type=bind,src={probe},dst=/data',image],capture_output=True,timeout=60)
                (out/'old-data-refusal.stderr').write_bytes(refusal.stderr)
                assert refusal.returncode!=0 and b'independent fresh directory' in refusal.stderr
                assert before=={p.name:digest(p) for p in probe.iterdir() if p.is_file()}
                r['checks']['old-data-boundary']={'status':'PASS','retained':str(retained),'sha256':before};persist()
            finally:old.close()
        variants={'baseline':[],'candidate':[],'no-quant':['MINIDB_QUANT_SCREEN=0'],'no-compact':['MINIDB_COMPACT_VECTOR=0'],'no-postings':['MINIDB_POSTINGS=0']}
        datasets={}
        for seed in (42,43,44):
            data=out/('data-'+str(seed));vectors,inserts,queries,truth=work.dataset(data,10000,128,1000,'random',seed)
            # Both versions receive exactly the same bare-vector SQL.
            inserts=[x.replace("'[",'[').replace("]'",']') for x in inserts];queries=[x.replace("'[",'[').replace("]'",']') for x in queries]
            (data/'workload.sql').write_text('\n'.join(inserts+queries)+'\n');datasets[seed]=(vectors,inserts,queries,truth)
        plan=[]
        for i,seed in enumerate((42,43,44)):
            order=list(variants) if i%2==0 else list(reversed(variants))
            plan += [('1c384m','1','384m',seed,name) for name in order]
        for label,cpu,memory in [('1c2g','1','2g'),('2c2g','2','2g'),('1c16m','1','16m')]:plan += [(label,cpu,memory,42,name) for name in ('baseline','candidate')]
        r['planned_runs']=len(plan);persist()
        for scenario,cpu,memory,seed,variant in plan:
            key=f'{scenario}-{seed}-{variant}'
            if any(x['key']==key and x['status']=='PASS' for x in r['runs']):continue
            vectors,inserts,queries,truth=datasets[seed];config=json.loads((frozen/'protocol_check/config.json').read_text());config.update(cpus=cpu,memory=memory,case_timeout=3600)
            target=r['images']['baseline' if variant=='baseline' else 'candidate'];sample={'key':key,'scenario':scenario,'seed':seed,'variant':variant,'image':target,'env':variants[variant],'status':'RUNNING','seconds':{},'resources':{}}
            r['runs'].append(sample);r['active']=key;persist();print('WORKLOAD',key,flush=True)
            session=VariantSession(config,out/(key+'.stderr'),image=target,extra_env=variants[variant]);observer=None
            def phase(name,fn):
                start=time.perf_counter();value=fn();sample['seconds'][name]=time.perf_counter()-start;return value
            def snap(name,drain=False):sample['resources'][name]=observer.snapshot(drain)
            try:
                phase('startup',session.start);observer=Observer(session);snap('startup')
                inspected=json.loads(resources.docker('inspect',session.name).stdout)[0]['HostConfig']
                assert inspected['NanoCpus']==int(cpu)*1000000000 and inspected['Memory']==int(memory[:-1])*(1024**3 if memory[-1]=='g' else 1024**2) and inspected['MemorySwap']==inspected['Memory']
                phase('schema',lambda:work.request(session,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(128))'))
                def write():
                    for sql in inserts:work.request(session,'BEGIN');assert work.request(session,sql)['affected_rows']==100;work.request(session,'COMMIT')
                phase('write',write);snap('write',True)
                phase('index',lambda:work.request(session,'CREATE VECTOR INDEX ix ON t(v)'));snap('index',True)
                first=phase('first_queries',lambda:[work.request(session,s)['rows'] for s in queries[:20]]);snap('first_queries')
                warm=phase('warmup',lambda:[work.request(session,s)['rows'] for s in queries[:20]]);snap('warmup')
                times={'filtered':[],'unfiltered':[]};answers=[]
                def query():
                    for i,sql in enumerate(queries):
                        start=time.perf_counter();answers.append(work.request(session,sql)['rows']);times['filtered' if i%2 else 'unfiltered'].append(time.perf_counter()-start)
                phase('query',query);snap('final',True)
                sample['seconds']['full']=sum(sample['seconds'].values());sample['latencies']=times
                sample['query_latency']={k:{'total':sum(v),'p50':statistics.median(v),'p95':percentile(v,.95),'p99':percentile(v,.99),'max':max(v)} for k,v in times.items()}
                final=sample['resources']['final'];sample['io_bytes']=resources.data_bytes(final);sample['peak_memory_bytes']=final['memory_peak_bytes']
                sample['cpu_stat']=final['cpu_stat']
                def cpu_seconds(snapshot):
                    stats=snapshot['cpu_stat']
                    return stats['usage_usec']/1e6 if stats.get('usage_usec') is not None else stats['usage_nsec']/1e9 if stats.get('usage_nsec') is not None else None
                before_cpu=cpu_seconds(sample['resources']['warmup']);after_cpu=cpu_seconds(final)
                sample['full_cpu_seconds']=after_cpu;sample['query_cpu_seconds']=after_cpu-before_cpu if after_cpu is not None and before_cpu is not None else None
                before_io=resources.data_bytes(sample['resources']['warmup']);sample['query_io_bytes']=sample['io_bytes']-before_io if sample['io_bytes'] is not None and before_io is not None else None
                assert not final['memory_events'].get('oom_kill',0)
                save(out/(key+'-answers.json'),answers);work.check_answers(answers,truth);work.check_answers(first,truth[:20]);work.check_answers(warm,truth[:20])
                observer.close();observer=None
                start=time.perf_counter();session.shutdown();sample['shutdown_seconds']=time.perf_counter()-start
                start=time.perf_counter();session.start();restart={'startup_seconds':time.perf_counter()-start}
                observer=Observer(session);again=[];latencies=[]
                for sql in queries[:20]:start=time.perf_counter();again.append(work.request(session,sql)['rows']);latencies.append(time.perf_counter()-start)
                work.check_answers(again,truth[:20]);restart['latencies']=latencies;restart['resources']=observer.snapshot(True);sample['restart']=restart
                observer.close();observer=None;work.persistence(session,vectors);sample['status']='PASS'
            except BaseException as e:
                sample.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
            finally:
                if observer:observer.close()
                session.close();persist()
        r['comparisons']={}
        for scenario in ('1c384m','1c2g','2c2g','1c16m'):
            for left,right in [('baseline','candidate')]+([('no-quant','candidate'),('no-compact','candidate'),('no-postings','candidate')] if scenario=='1c384m' else []):
                entry={}
                for metric in ('write','query','full','io_bytes','peak_memory_bytes','full_cpu_seconds','query_cpu_seconds','query_io_bytes'):
                    values={v:[x['seconds'].get(metric,x.get(metric)) for x in r['runs'] if x['status']=='PASS' and x['scenario']==scenario and x['variant']==v] for v in (left,right)}
                    med={v:statistics.median(a) if a and all(x is not None for x in a) else None for v,a in values.items()}
                    entry[metric]={'medians':med,'change_percent':(med[right]/med[left]-1)*100 if med[left] and med[right] is not None else None,'raw':values}
                r['comparisons'][scenario+'-'+left+'-to-'+right]=entry
        assert files()==r['sha256'],'sources changed during validation'
        r['status']='PASS';save(out/'summary.json',r['comparisons'])
    except BaseException as e:r.update(status='FAILED_OR_BLOCKED',error=repr(e),traceback=traceback.format_exc());print(r['error'],flush=True)
    finally:
        r['conversation_unchanged']=digest(REPO/'agent/conversation.json')==r['conversation_sha256'];r['completed_runs']=sum(x['status']=='PASS' for x in r['runs']);persist()
        print('REPORT:',out/'report.json');print('STATUS:',r['status']);print('COMPLETED_RUNS:',r['completed_runs'])
    return 0 if r['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
