#!/usr/bin/env python3
"""Freeze e5/candidate, build offline, gate paired benchmarks on correctness.

No changes to database/conversation files, no commits, no package downloads.
Main comparison: 1c/2g, three alternating pairs. Other specs: one pair each.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time
import traceback

REPO=Path(__file__).resolve().parents[1]
BASE='e5ad115b0ca35776a44cd6ec31b52e9efe2af01f'
BUILDER='gcc:12-bookworm@sha256:44482187aa3865ad1e2477dc8c56bae4fd8e071e7d99c5f44956effc7f7cc846'


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_paths(root):
    return [root/n for n in ('Dockerfile','.dockerignore','requirements.lock')]+sorted(
        p for name in ('minidb','native') for p in (root/name).rglob('*')
        if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.so','.pyc','.o'))


def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def percentile(values,p):return sorted(values)[max(0,math.ceil(len(values)*p)-1)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--resume',type=Path,help='existing report; hashes must still match')
    args=parser.parse_args()
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=(args.resume.parent if args.resume else args.output or REPO.parent/'reports'/('full-native-docker-'+stamp)).resolve()
    if args.resume:r=json.loads(args.resume.read_text())
    else:
        out.mkdir(parents=True,exist_ok=False)
        r={'status':'STARTED','baseline':BASE,'working_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
           'source_sha256':{str(p.relative_to(REPO)):digest(p) for p in runtime_paths(REPO)},
           'harness_sha256':{str(p.relative_to(REPO)):digest(p) for folder in ('checks','protocol_check','mysqltest') for p in (REPO/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts},
           'conversation_sha256_at_start':digest(REPO/'agent/conversation.json'),
           'checks':{},'images':{},'runs':[],
           'limitations':['Default dataset matches documented seed/shape; original document SQL and raw dataset hashes were not provided.',
             'No global page-cache purge; first-query and warmup windows are separately included in full-stage time.',
             '1c2g uses three paired rounds; other resource groups have one pair, preliminary only.',
             'Platform E formula/hidden workload unknown; no extrapolation to score.',
             'Independent oracle and resource observer stay outside measured cgroup and request timing.',
             'Restart startup/first-query metrics are separate supplementary windows; OS page cache is not claimed cold.',
             'Runtime differs from document by materialization CRC validation; its cost is included.']}
    r['status']='STARTED'
    r.pop('error',None);r.pop('traceback',None)
    def persist():save(out/'report.json',r)
    def run(name,argv,cwd=None,timeout=3600):
        if r['checks'].get(name,{}).get('status')=='PASS':return
        if name in r['checks']:
            history=out/'retry-history';history.mkdir(exist_ok=True)
            oldlog=out/(name+'.log')
            if oldlog.exists():shutil.copyfile(oldlog,history/(name+'-'+stamp+'.log'))
            words=list(map(str,argv))
            if '--output' in words:
                previous=Path(words[words.index('--output')+1])
                if previous.exists():
                    assert previous.parent==out
                    previous.rename(history/(name+'-'+stamp))
        print('CHECK',name,flush=True);r['active']=name;persist();save(out/(name+'.command.json'),[str(x) for x in argv])
        with (out/(name+'.log')).open('wb') as log:
            result=subprocess.run(list(map(str,argv)),cwd=cwd or REPO,stdout=log,stderr=subprocess.STDOUT,timeout=timeout)
        r['checks'][name]={'status':'PASS' if result.returncode==0 else 'FAIL','returncode':result.returncode,'log':name+'.log'};persist()
        if result.returncode:raise RuntimeError(name+': '+(out/(name+'.log')).read_text(errors='replace')[-3000:])
    def assert_sources():
        assert r['source_sha256']=={str(p.relative_to(REPO)):digest(p) for p in runtime_paths(REPO)},'runtime changed; create a new frozen run'
        assert all((REPO/p).is_file() and digest(REPO/p)==h for p,h in r['harness_sha256'].items()),'harness changed; create a new run'
    try:
        assert_sources();persist()
        contexts=out/'contexts';candidate=contexts/'candidate';baseline=contexts/'e5'
        if not candidate.exists():
            candidate.mkdir(parents=True);baseline.mkdir()
            for p in r['source_sha256']:
                target=candidate/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/p,target)
            for p in r['harness_sha256']:
                target=candidate/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/p,target)
            paths=subprocess.check_output(['git','ls-tree','-r','--name-only',BASE,'Dockerfile','.dockerignore','requirements.lock','minidb','native'],cwd=REPO,text=True).splitlines()
            for p in paths:
                target=baseline/p;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(subprocess.check_output(['git','show',BASE+':'+p],cwd=REPO))
        for item in json.loads((candidate/'native/vendor/manifest.json').read_text()):
            assert digest(candidate/'native/vendor'/item['path'])==item['sha256']
        for path,expected in {**r['source_sha256'],**r['harness_sha256']}.items():
            assert digest(candidate/path)==expected,('frozen file changed',path)
        baseline_hashes={str(p.relative_to(baseline)):digest(p) for p in runtime_paths(baseline)}
        if 'baseline_source_sha256' in r:assert r['baseline_source_sha256']==baseline_hashes,'frozen baseline changed'
        r['baseline_source_sha256']=baseline_hashes;persist()
        run('docker-version',['docker','version'],timeout=60)
        for label,context in [('e5',baseline),('candidate',candidate)]:
            run(label+'-build',['docker','build','--platform=linux/amd64','--no-cache','--network=none','--iidfile',out/(label+'.id'),'-t','minivectordb:full-native-'+label+'-'+out.name.lower(),'.'],context)
            r['images'][label]=(out/(label+'.id')).read_text().strip()
            run(label+'-inspect',['docker','image','inspect',r['images'][label]],timeout=60)
        image=r['images']['candidate'];inspected=json.loads((out/'candidate-inspect.log').read_text())[0]
        assert inspected['Config']['Entrypoint']==['/app/minivectordb-native'];persist()
        run('e5-public',[sys.executable,'-B',candidate/'protocol_check/run.py','--image',r['images']['e5'],'--output',out/'e5-public'],candidate)
        # Build the test interposer in the same fixed toolchain, without adding it
        # to the production image. No capability relaxation for database tests.
        fault=out/'fault';fault.mkdir(exist_ok=True)
        run('fault-build',['docker','run','--rm','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,nosuid,size=64m',
            '--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={candidate},dst=/src,readonly','--mount',f'type=bind,src={fault},dst=/out',BUILDER,
            'gcc','-shared','-fPIC','-O2','/src/checks/native_journal_fault.c','-ldl','-o','/out/fault.so'])
        r['fault_sha256']=digest(fault/'fault.so')
        build_test=['docker','run','--rm','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,nosuid,size=128m',
            '--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={candidate},dst=/src,readonly','--mount',f'type=bind,src={fault},dst=/out',BUILDER]
        run('kernel-build',build_test+['gcc','-std=c11','-O3','-ffp-contract=off','-fno-fast-math','/src/checks/native_fixed128.c','-lm','-o','/out/kernel-check'])
        run('codec-build',build_test+['bash','-c',
            'set -e\ngcc -O2 -c /src/native/screen.c -ffp-contract=off -fno-fast-math -o /tmp/screen.o\n'
            'gcc -O2 -c /src/native/vendor/yyjson/yyjson.c -o /tmp/json.o\n'
            'gcc -O2 -DUTF8PROC_STATIC -c /src/native/vendor/utf8proc/utf8proc.c -o /tmp/unicode.o\n'
            'g++ -std=c++17 -O2 -static-libstdc++ -static-libgcc -ffp-contract=off -fno-fast-math /src/checks/native_codec.cpp /tmp/screen.o /tmp/json.o /tmp/unicode.o -lm -lz -o /out/codec-check'])
        for binary in ('kernel-check','codec-check'):
            run(binary,['docker','run','--rm','--network=none','--cpus=1','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                '--mount',f'type=bind,src={fault},dst=/tests,readonly','--entrypoint','/tests/'+binary,image])
        # All following checks refer to the one candidate image ID, never a tag.
        for memory in ('2g','12m'):
            label='journal-1c-'+memory
            run(label,[sys.executable,'-B',candidate/'checks/native_journal.py','--image',image,'--fault-library',fault/'fault.so','--cpus','1','--memory',memory,'--output',out/label],candidate)
        for cpu in ('1','2'):
            for memory in ('12m','64m','2g'):
                label='public-'+cpu+'c-'+memory;cfg=json.loads((candidate/'protocol_check/config.json').read_text());cfg.update(cpus=cpu,memory=memory)
                save(out/(label+'-config.json'),cfg)
                run(label,[sys.executable,'-B',candidate/'protocol_check/run.py','--image',image,'--config',out/(label+'-config.json'),'--output',out/label],candidate)
        checks=[('normal','1','2g','all',[]),('scalar','1','2g','topk',['--scalar']),('buffered','1','16m','plans',['--no-mmap']),
                ('stress12-1','1','12m','stress',[]),('stress12-2','2','12m','stress',[]),('model-2','2','2g','model',[])]
        for label,cpu,memory,section,extra in checks:
            run(label,[sys.executable,'-B',candidate/'checks/native_validation.py','--image',image,'--cpus',cpu,'--memory',memory,'--section',section,'--output',out/label]+extra,candidate)
        run('contract',[sys.executable,'-B',candidate/'checks/contract.py','--image',image,'--output',out/'contract'],candidate)
        # There is intentionally no performance bypass when correctness fails.
        sys.path.insert(0,str(candidate/'protocol_check'));sys.path.insert(0,str(candidate/'checks'))
        from _transport import Session
        work=load_module('frozen_workload',candidate/'checks/optimization_workload.py')
        resources=load_module('frozen_resources',candidate/'checks/optimization_resources.py')
        resources.OUT=out;resources.HERE=candidate/'checks';resources.IMAGE=image
        if not (out/'data/oracle.json').exists():
            vectors,inserts,queries,truth=work.dataset(out/'data',10000,128,1000,'random',872909)
        else:
            # Regenerate deterministically, verify against the persisted oracle.
            regen=out/('data-recheck-'+stamp)
            vectors,inserts,queries,truth=work.dataset(regen,10000,128,1000,'random',872909)
            assert digest(regen/'dataset.json')==digest(out/'data/dataset.json') and truth==json.loads((out/'data/oracle.json').read_text())
        r['dataset']=json.loads((out/'data/dataset.json').read_text());persist()
        specs=[('1c2g','1','2g',3),('2c2g','2','2g',1),('1c12m','1','12m',1),('2c12m','2','12m',1),('1c64m','1','64m',1),('2c64m','2','64m',1)]
        for scenario,cpu,memory,rounds in specs:
            config=json.loads((candidate/'protocol_check/config.json').read_text());config.update(cpus=cpu,memory=memory,case_timeout=1800)
            resources.CONFIG=config;resources.MEMORY_LIMIT=int(memory[:-1])*(1024**3 if memory[-1]=='g' else 1024**2)
            for pair in range(rounds):
                for version in (['e5','candidate'] if pair%2==0 else ['candidate','e5']):
                    key=f'{scenario}-pair{pair+1}-{version}'
                    if any(x['key']==key and x['status']=='PASS' for x in r['runs']):continue
                    print('WORKLOAD',key,flush=True);sample={'key':key,'scenario':scenario,'pair':pair+1,'version':version,'image':r['images'][version],'status':'RUNNING','seconds':{},'resources':{}}
                    r['runs'].append(sample);r['active']=key;persist()
                    session=Session(config,out/(key+'.stderr'),image=r['images'][version]);observer=None
                    def phase(name,fn):
                        sample['active_phase']=name
                        begin=time.perf_counter();value=fn();sample['seconds'][name]=time.perf_counter()-begin;return value
                    def snap(name,drain=False):sample['resources'][name]=observer.snapshot(drain)
                    try:
                        phase('startup',session.start);observer=resources.Observer(session);snap('after_startup')
                        phase('schema',lambda:work.request(session,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(128))'))
                        def write():
                            for batch,sql in enumerate(inserts):
                                sample['active_batch']=batch
                                work.request(session,'BEGIN');assert work.request(session,sql)['affected_rows']==100;work.request(session,'COMMIT')
                        phase('write',write);snap('after_write',True)
                        phase('index',lambda:work.request(session,'CREATE VECTOR INDEX ix ON t(v)'));snap('after_index',True)
                        first=phase('first_queries',lambda:[work.request(session,sql)['rows'] for sql in queries[:20]]);snap('after_first_queries')
                        warm=phase('warmup',lambda:[work.request(session,sql)['rows'] for sql in queries[:20]]);snap('after_warmup')
                        times={'filtered':[],'unfiltered':[]};answers=[]
                        def query():
                            for i,sql in enumerate(queries):
                                sample['active_query_index']=i
                                begin=time.perf_counter();answers.append(work.request(session,sql)['rows']);times['filtered' if i%2 else 'unfiltered'].append(time.perf_counter()-begin)
                        phase('query',query);snap('final',True)
                        sample['seconds']['full']=sum(sample['seconds'].values())
                        sample['query_latency']={k:{'total_seconds':sum(v),'p50':statistics.median(v),'p95':percentile(v,.95),'p99':percentile(v,.99),'max':max(v)} for k,v in times.items()}
                        sample['raw_latencies_seconds']=times
                        sample['io_bytes']=resources.data_bytes(sample['resources']['final'])
                        sample['peak_memory_bytes']=sample['resources']['final']['memory_peak_bytes']
                        assert sample['io_bytes'] is not None,'data-device I/O counters unavailable'
                        assert not sample['resources']['final']['memory_events'].get('oom_kill',0)
                        save(out/(key+'-answers.json'),answers)
                        work.check_answers(answers,truth);work.check_answers(first,truth[:20]);work.check_answers(warm,truth[:20])
                        observer.close();observer=None
                        # Process-cold restart is measured separately, including
                        # materialization validation/replay. Do not mix this extra
                        # window into the document's primary T/B/M comparison.
                        sample['active_phase']='restart_probe';session.shutdown();restart={'latencies_seconds':[]}
                        begin=time.perf_counter();session.start();restart['startup_seconds']=time.perf_counter()-begin
                        observer=resources.Observer(session);restart['after_startup']=observer.snapshot()
                        again=[]
                        for sql in queries[:20]:
                            begin=time.perf_counter();again.append(work.request(session,sql)['rows']);restart['latencies_seconds'].append(time.perf_counter()-begin)
                        restart['first_queries_seconds']=sum(restart['latencies_seconds']);restart['final']=observer.snapshot(True)
                        restart['io_bytes']=resources.data_bytes(restart['final']);restart['peak_memory_bytes']=restart['final']['memory_peak_bytes']
                        work.check_answers(again,truth[:20]);sample['restart_probe']=restart
                        observer.close();observer=None
                        # Full row persistence, failed statements and rollback are
                        # checked only after ending the measurement window.
                        work.persistence(session,vectors)
                        sample['status']='PASS'
                    except BaseException as e:
                        sample['status']='FAIL';sample['error']=repr(e)
                        if session.proc is not None:
                            state=subprocess.run(['docker','inspect',session.name],capture_output=True,text=True)
                            (out/(key+'-failure-inspect.json')).write_text(state.stdout)
                        raise
                    finally:
                        if observer:observer.close()
                        session.close();persist()
        comparisons={}
        for scenario,_,_,rounds in specs:
            matched={version:[x for x in r['runs'] if x['scenario']==scenario and x['version']==version and x['status']=='PASS'] for version in ('e5','candidate')}
            assert all(len(v)==rounds for v in matched.values())
            metrics={}
            for metric in ('write','query','full','io_bytes','peak_memory_bytes'):
                med={v:statistics.median(x['seconds'][metric] if metric in ('write','query','full') else x[metric] for x in samples) for v,samples in matched.items()}
                metrics[metric]={**med,'change_percent':(med['candidate']/med['e5']-1)*100 if med['e5'] else None}
            comparisons[scenario]={'pairs':rounds,'medians':metrics,'single_pair_only':rounds==1}
        r['comparisons']=comparisons;r['completed_runs']=sum(x['status']=='PASS' for x in r['runs'])
        lines=['最终候选对 e5：三轮主对照及单对补测；不换算平台 E。',
            '全阶段时间含启动、schema、写入/提交、索引、首次查询、预热和正式查询；观察器/真值在计时外。',
            '完整 I/O 含实际写回，峰值含页缓存。重启补测窗口单列，不冒充文件系统冷缓存。']
        for scenario,entry in comparisons.items():
            lines.append(f'\n{scenario}: {entry["pairs"]} 对')
            for metric,value in entry['medians'].items():
                change='unknown' if value['change_percent'] is None else f'{value["change_percent"]:+.2f}%'
                lines.append(f'  {metric}: {value["e5"]:.6f} -> {value["candidate"]:.6f}, {change}')
            for group in ('unfiltered','filtered'):
                for metric in ('p95','p99','max'):
                    med={v:statistics.median(x['query_latency'][group][metric] for x in r['runs'] if x['status']=='PASS' and x['scenario']==scenario and x['version']==v) for v in ('e5','candidate')}
                    lines.append(f'  {group} {metric}: {med["e5"]*1000:.4f} -> {med["candidate"]*1000:.4f} ms')
            for metric in ('startup_seconds','first_queries_seconds','io_bytes','peak_memory_bytes'):
                med={}
                for v in ('e5','candidate'):
                    values=[x['restart_probe'][metric] for x in r['runs'] if x['status']=='PASS' and x['scenario']==scenario and x['version']==v]
                    med[v]=f'{statistics.median(values):.6f}' if all(x is not None for x in values) else 'unknown'
                lines.append(f'  restart {metric}: {med["e5"]} -> {med["candidate"]}')
        (out/'summary.zh.txt').write_text('\n'.join(lines)+'\n')
        assert_sources();r['status']='PASS'
    except BaseException as e:
        r['status']='FAILED_OR_BLOCKED';r['error']=repr(e);r['traceback']=traceback.format_exc();print(r['error'],flush=True)
    finally:
        r['completed_runs']=sum(x['status']=='PASS' for x in r['runs'])
        r['conversation_unchanged']=digest(REPO/'agent/conversation.json')==r['conversation_sha256_at_start']
        persist();print('REPORT:',out/'report.json');print('STATUS:',r['status']);print('COMPLETED_RUNS:',r['completed_runs'])
    return 0 if r['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
