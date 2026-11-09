#!/usr/bin/env python3
"""Reopen the documented k%97 vector corpus and build/query the migrated index."""
import argparse
import hashlib
import heapq
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from evaluation.stress_run import Process
from evaluation.driver import Client

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rows',type=int,default=140000)
    parser.add_argument('--image',default='minivectordb:storage-migration')
    args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=False)
    data=args.output/'data';shutil.copytree(args.source,data)
    subprocess.run(['sync','-f',str(data)],check=True)
    for path in data.iterdir():
        if path.is_file():
            with path.open('rb') as source:os.posix_fadvise(source.fileno(),0,0,os.POSIX_FADV_DONTNEED)
    name='minivector-migrated-resource-'+uuid.uuid4().hex
    command=['docker','run','-i','--name',name,'--network','none','--cpus','2',
             '--memory','64m','--memory-swap','64m','-v',str(data)+':/data',args.image]
    report={'scope':'64 MiB compatibility and resource probe; source vectors are [(key%97-48)/8]*128, tag=key%7',
            'source_manifest_sha256':hashlib.sha256((args.source/'manifest.json').read_bytes()).hexdigest(),
            'source_bytes':sum(path.stat().st_size for path in data.iterdir() if path.is_file()),
            'rows':args.rows,'memory_limit_bytes':67108864,'image_id':subprocess.check_output(['docker','inspect','-f','{{.Id}}',args.image],text=True).strip()}
    def save():(args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    save();client=None
    with (args.output/'stderr.txt').open('w') as log:
        try:
            client=Process(command,data,log,timeout=180)
            def execute(sql):
                actual=client.execute(sql)
                if actual.get('error'):raise AssertionError(actual)
                return actual
            execute('CREATE VECTOR INDEX migrated ON t(v)')
            report['topk_checks']=0
            for filtered,q in ((False,0.),(True,.5),(True,-.5)):
                where=' WHERE tag=3' if filtered else ''
                vector='['+','.join([str(q)]*128)+']'
                actual=execute('SELECT id FROM t'+where+' ORDER BY L2_DISTANCE(v,'+vector+') LIMIT 10')
                expected=heapq.nsmallest(10,(key for key in range(1,args.rows+1) if not filtered or key%7==3),
                                        key=lambda key:(abs((key%97-48)/8-q),key))
                assert [row[0] for row in actual['rows']]==expected
                report['topk_checks']+=1
            actual=execute('SELECT id FROM t')
            assert sorted(row[0] for row in actual['rows'])==list(range(1,args.rows+1))
            report['full_scan_rows']=len(actual['rows'])
            pid=int(subprocess.check_output(['docker','inspect','-f','{{.State.Pid}}',name],text=True))
            report['resources']=Client.metrics(SimpleNamespace(pid=pid))
            membership=Path('/proc',str(pid),'cgroup').read_text()
            relative=next(line.split('::',1)[1] for line in membership.splitlines() if line.startswith('0::'))
            events=Path('/sys/fs/cgroup',relative.lstrip('/'),'memory.events').read_text()
            report['memory_events']={key:int(value) for key,value in (line.split() for line in events.splitlines())}
            assert report['resources']['memory_peak_bytes']<=report['memory_limit_bytes']
            assert report['memory_events'].get('oom_kill',0)==0
            report['status']='PASS'
        except BaseException as error:
            report['status']='FAIL';report['error']=repr(error);save();raise
        finally:
            try:
                if client is not None:client.stop(kill=report.get('status')!='PASS')
            finally:
                state=json.loads(subprocess.check_output(['docker','inspect','-f','{{json .State}}',name],text=True))
                report['exit_code']=state['ExitCode'];report['oom_killed']=state['OOMKilled']
                subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                save()
    print(json.dumps(report,indent=2))
if __name__=='__main__':main()
