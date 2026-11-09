#!/usr/bin/env python3
"""Build and run the local MiniVectorDB acceptance suite and write report.json."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evaluation.driver import Client
from evaluation.cases import run_cases


class Session:
    def __init__(self,args,directory):
        self.args=args;self.directory=directory;self.generation=0;self.client=None
        self.restart()
    def restart(self,kill=False):
        if self.client is not None:self.client.close(kill=kill)
        self.client=Client(self.directory/'data',self.directory/f'process-{self.generation}',
                           image=self.args.image if self.args.docker else None,
                           memory=self.args.memory,timeout=self.args.timeout)
        self.generation+=1
    def execute(self,*args,**kwargs):return self.client.execute(*args,**kwargs)
    def metrics(self):return self.client.metrics()
    def close(self):
        if self.client is not None:
            self.client.close();self.client=None


def source_hash(paths):
    digest=hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode());digest.update(b'\0');digest.update(path.read_bytes());digest.update(b'\0')
    return digest.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker',action='store_true',help='Use offline constrained Docker containers')
    parser.add_argument('--image',default='minivectordb:local-evaluation')
    parser.add_argument('--skip-build',action='store_true')
    parser.add_argument('--benchmark',action='store_true')
    parser.add_argument('--resource',action='store_true')
    parser.add_argument('--rows',type=int,default=10000)
    parser.add_argument('--queries',type=int,default=1000)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--memory',default='128m')
    parser.add_argument('--timeout',type=float,default=30)
    parser.add_argument('--strict-peak',action='store_true',default=True,help='Require raw memory.peak<=memory.max in resource tests (always enabled)')
    parser.add_argument('--keep-data',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts'/time.strftime('%Y%m%d-%H%M%S'))
    args=parser.parse_args()
    if args.rows<100 or args.queries<2 or args.rounds<1:parser.error('rows>=100, queries>=2, rounds>=1 required')
    if args.resource and not args.docker:parser.error('--resource requires --docker')
    args.output=args.output.resolve()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'schema':'minivectordb-local-evaluation-v1','scope':'LOCAL ONLY; no official driver or official score',
            'resource_policy':'memory.max enforced, raw memory.peak<=memory.max required, swap disabled, no OOM events',
            'status':'RUNNING','git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            'working_tree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip()),
            'implementation_sha256':source_hash([ROOT/'Dockerfile',ROOT/'requirements.txt',ROOT/'requirements.lock',ROOT/'debian-packages.lock',*sorted((ROOT/'minivectordb').glob('*.py')),*sorted((ROOT/'native').glob('*.c'))]),
            'harness_sha256':source_hash([Path(__file__),*sorted((ROOT/'evaluation').glob('*.py'))]),
            'public_cases_sha256':source_hash([ROOT/'scripts/check_public.py',*sorted((ROOT/'mysqltest').rglob('*.test')),*sorted((ROOT/'mysqltest').rglob('*.result')),ROOT/'mysqltest/manifest.json']),
            'configuration':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()},'phases':{},'cases':[]}
    started=time.perf_counter()
    def save():
        report['elapsed_seconds']=time.perf_counter()-started
        temporary=args.output/'report.tmp'
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
        temporary.replace(args.output/'report.json')
    def phase(name,command,env=None):
        print(name,flush=True);begin=time.perf_counter()
        with (args.output/(name+'.log')).open('w') as log:
            process=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,env=env)
        report['phases'][name]={'status':'PASS' if process.returncode==0 else 'FAIL','seconds':time.perf_counter()-begin,'log':name+'.log'}
        save()
        if process.returncode:raise RuntimeError(name+' failed; see '+name+'.log')
    def session(name):return Session(args,args.output/name)
    def case(name,callback):
        print('case: '+name,flush=True);begin=time.perf_counter();client=session(name)
        result={'name':name,'status':'RUNNING'};report['cases'].append(result);save()
        try:
            callback(client);result['resources']=client.metrics();result['status']='PASS'
        except BaseException:
            result['status']='FAIL';result['error']=traceback.format_exc();raise
        finally:
            client.close();result['seconds']=time.perf_counter()-begin;save()
        if not args.keep_data:shutil.rmtree(args.output/name/'data')
    try:
        if args.docker and not args.skip_build:
            phase('build',['docker','build','--platform','linux/amd64','-t',args.image,'.'])
        if args.docker:
            report['image_id']=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',args.image],text=True).strip()
            phase('regression',['docker','run','--rm','--network','none','--cpus','2','--memory',args.memory,'--memory-swap',args.memory,
                               '-v',str(ROOT/'tests')+':/app/tests:ro','--entrypoint','python3',args.image,'-m','unittest','discover','-s','tests','-v'])
        else:phase('regression',[sys.executable,'-m','unittest','discover','-s','tests','-v'])
        phase('harness-self-test',[sys.executable,'-m','unittest','evaluation.test_harness','-v'])
        phase('material-format',[sys.executable,'scripts/check_conversation.py'])
        if args.docker:
            phase('public-cases',['docker','run','--rm','--network','none','--cpus','2','--memory',args.memory,'--memory-swap',args.memory,
                  '-v',str(ROOT/'scripts')+':/checks:ro','-v',str(ROOT/'mysqltest')+':/cases:ro',
                  '-v',str(args.output)+':/reports','--entrypoint','python3',args.image,
                  '/checks/check_public.py','--cases','/cases','--report','/reports/public-report.json'])
        else:
            phase('public-cases',[sys.executable,'scripts/check_public.py','--cases',str(ROOT/'mysqltest'),
                  '--data-root',str(args.output/'public-data'),'--report',str(args.output/'public-report.json')])
        run_cases(case)
        from evaluation.fixtures import run_fixture
        for fixture in sorted((ROOT/"tests"/"sql").glob("*.test")):
            case("fixture-"+fixture.stem,lambda client,path=fixture:run_fixture(client,path))
        if args.benchmark:
            from evaluation.benchmark import run_round
            report['benchmark']=[]
            for number in range(1,args.rounds+1):
                print(f'benchmark: round {number}/{args.rounds}',flush=True)
                result=run_round(session,719+number,args.rows,args.queries,number)
                report['benchmark'].append(result);save()
                if not args.keep_data:
                    for track in ('write','query'):shutil.rmtree(args.output/f'benchmark-{number}-{track}'/'data')
            report['benchmark_summary']={'write_seconds_median':statistics.median(r['write']['seconds'] for r in report['benchmark']),
                                         'query_seconds_median':statistics.median(r['query']['seconds'] for r in report['benchmark']),
                                         'official_E':None,'reason':'No frozen official baseline or dedicated data-device configuration is available.'}
        if args.resource:
            data=args.output/'resource-data';data.mkdir()
            phase('resource',['docker','run','--rm','--network','none','--cpus','2','--memory','64m','--memory-swap','64m','-e','MINIVECTOR_STRICT_PEAK='+str(int(args.strict_peak)),
                              '-v',str(data)+':/data','-v',str(ROOT/'tests')+':/app/tests:ro','--entrypoint','python3',args.image,'-m','tests.resource_container'])
            if not args.keep_data:shutil.rmtree(data)
            data=args.output/'resource-index-data';data.mkdir()
            phase('resource-index',['docker','run','--rm','--network','none','--cpus','2','--memory','64m','--memory-swap','64m','-e','MINIVECTOR_STRICT_PEAK='+str(int(args.strict_peak)),
                                    '-v',str(data)+':/data','-v',str(ROOT/'tests')+':/app/tests:ro','--entrypoint','python3',args.image,'-m','tests.resource_index_container'])
            if not args.keep_data:shutil.rmtree(data)
        report['status']='PASS'
    except BaseException as error:
        report['status']='FAIL';report['error']=traceback.format_exc()
        print(str(error),file=sys.stderr)
    finally:save()
    print(str(args.output/'report.json'),flush=True)
    return 0 if report['status']=='PASS' else 1

if __name__=='__main__':raise SystemExit(main())
