#!/usr/bin/env python3
"""Large-data correctness in 64 MiB database containers; driver stays on host."""
import argparse
import heapq
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from evaluation.driver import Client
from evaluation.stress_run import Process,decode_text

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ours',default='minivectordb:team');parser.add_argument('--teammate',default='minivectordb:teammate-e5af6d16')
parser.add_argument('--rows',type=int,default=140000);parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
report={'scope':'common bare-vector SQL; 64 MiB database cgroup, host driver excluded; large-data correctness only',
        'rows':args.rows,'dimension':128,'memory_limit':64*1024*1024,'results':{}}
def save():(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
for version,image in [('ours',args.ours),('teammate',args.teammate)]:
    result={'status':'RUNNING','image':image};report['results'][version]=result;save()
    directory=args.output/version;directory.mkdir();data=directory/'data';data.mkdir()
    name='minivector-resource-'+uuid.uuid4().hex
    command=['docker','run','-i','--name',name,'--platform','linux/amd64','--network','none',
             '--cpus','2','--memory','64m','--memory-swap','64m','-v',str(data)+':/data',image]
    with (directory/'stderr.log').open('w') as log:
        client=None
        try:
            client=Process(command,data,log,'json' if version=='ours' else 'text',timeout=180)
            def execute(sql):
                actual=client.execute(sql)
                if actual.get('error'):raise AssertionError(actual)
                return actual
            execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(128))')
            for start in range(0,args.rows,100):
                values=[]
                for key in range(start+1,min(start+101,args.rows+1)):
                    x=(key%97-48)/8
                    vector='['+','.join([str(x)]*128)+']'
                    values.append(f'({key},{key%7},{vector})')
                execute('INSERT INTO t VALUES '+','.join(values))
                if start%20000==0:print(version,'loaded',start+len(values),flush=True)
            result['data_bytes']=sum(p.stat().st_size for p in data.rglob('*') if p.is_file())
            result['data_larger_than_memory']=result['data_bytes']>report['memory_limit']
            # Reopen the confirmed data before queries, with the same 64 MiB cap.
            client.stop();subprocess.run(['docker','rm',name],check=True,stdout=subprocess.DEVNULL)
            client=Process(command,data,log,'json' if version=='ours' else 'text',timeout=180)
            result['topk_correct']=0
            for filtered,q in [(False,0.),(True,.5),(True,-.5)]:
                where=' WHERE tag=3' if filtered else ''
                vector='['+','.join([str(q)]*128)+']'
                actual=execute('SELECT id FROM t'+where+' ORDER BY L2_DISTANCE(v,'+vector+') LIMIT 10')
                if actual.get('text_rows'):decode_text(actual,{'rows':[[1]]})
                candidates=(key for key in range(1,args.rows+1) if not filtered or key%7==3)
                expected=heapq.nsmallest(10,candidates,key=lambda key:(abs((key%97-48)/8-q),key))
                if [row[0] for row in actual['rows']]!=expected:raise AssertionError('large-data TopK mismatch')
                result['topk_correct']+=1
            pid=int(subprocess.check_output(['docker','inspect','-f','{{.State.Pid}}',name],text=True))
            result['resources_before_full_scan']=Client.metrics(SimpleNamespace(pid=pid));save()
            actual=execute('SELECT id FROM t')
            if actual.get('text_rows'):decode_text(actual,{'rows':[[1]]})
            if sorted(row[0] for row in actual['rows'])!=list(range(1,args.rows+1)):raise AssertionError('full scan content mismatch')
            result['full_scan_rows']=len(actual['rows']);result['status']='PASS'
        except Exception as error:
            result['status']='FAIL';result['error']=repr(error)
        finally:
            if client is not None:
                try:client.stop(kill=result['status']!='PASS')
                except Exception as error:result['close_error']=repr(error)
            state=json.loads(subprocess.check_output(['docker','inspect','-f','{{json .State}}',name],text=True))
            result['exit_code']=state['ExitCode'];result['oom_killed']=state['OOMKilled']
            if state['OOMKilled']:result['status']='FAIL'
            subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            save()
    print(version,result['status'],flush=True)
print(json.dumps(report,indent=2))
raise SystemExit(0 if all(r['status']=='PASS' for r in report['results'].values()) else 1)
