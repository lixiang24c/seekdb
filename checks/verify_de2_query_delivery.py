#!/usr/bin/env python3
"""Correctness-only gate for the de2 query migration. One pass per configuration.
Build root Dockerfile offline, preserve inputs, use one immutable image. No
benchmark, repository rewrites, commits, pushes, or formal submission.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import traceback
from verify_queryable import REPO, BUILDER, files, digest, save
from verify_queryable_delivery import fingerprint

DISABLED=['MINIDB_'+x+'=0' for x in ('QUERY_PLANS','DIRECT_NUMBERS','FUSED_LITERAL','VECTOR_BOUNDARY','BLOCK_SCAN','CERTIFY','UNIFORM_SCAN','REQUEST_VIEW','BATCH_OUTPUT','PROJECTED_LOOKUP','POINT_KEYS','SELECTIVE_LOOKUP','LARGE_MAP','COMPACT_BUDGET')]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path);p.add_argument('--resume',type=Path);p.add_argument('--plan',action='store_true');args=p.parse_args()
    if args.plan:
        print(__doc__);print('1c2g: public, contract, numeric/protocol/model/stress, focused truth/plans/CRC, recovery. 2c2g: public + focused. 1c12m/16m: truth/plans; 1c64m: mmap/SIMD off; 1c32m: >64MiB log. Same image: disabled switches. No performance runs.');return 0
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=(args.resume.parent if args.resume else args.output or REPO.parent/'reports'/('de2-query-docker-'+stamp)).resolve()
    if args.resume:report=json.loads(args.resume.read_text())
    else:
        out.mkdir(parents=True,exist_ok=False)
        report={'status':'STARTED','head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),'baseline':'de2e28229ceec1d1350a7fa2a878e4898639ee87','branch':subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip(),'input_sha256':files(),'conversation_sha256_before':digest(REPO/'agent/conversation.json'),'image':None,'checks':{},'performance_runs':0,'formal_submission':False,'committed':False,'pushed':False}
    frozen=out/'source'
    def persist():save(out/'report.json',report)
    def run(name,cmd,timeout=3600,cwd=None):
        if report['checks'].get(name,{}).get('status')=='PASS':return
        cmd=list(map(str,cmd));log=out/(name+'.log')
        if '--output' in cmd:
            dest=Path(cmd[cmd.index('--output')+1])
            if dest.is_relative_to('/out'):dest=out/dest.relative_to('/out')
            if dest.exists():
                assert dest.parent==out
                history=out/'retry-history';history.mkdir(exist_ok=True);dest.rename(history/(name+'-'+stamp))
        if log.exists():shutil.copyfile(log,out/(name+'-'+stamp+'.previous.log'))
        report['active']=name;persist();save(out/(name+'.command.json'),cmd);print('CHECK:',name,flush=True)
        with log.open('wb') as f:
            result=subprocess.run(cmd,cwd=cwd or frozen,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
        report['checks'][name]={'status':'PASS' if result.returncode==0 else 'FAIL','returncode':result.returncode,'log':log.name};persist()
        if result.returncode:raise RuntimeError(name+': '+log.read_text(errors='replace')[-4000:])
    try:
        assert files()==report['input_sha256'],'source or harness changed; start a fresh report'
        if report['status']=='PASS':print('Already completed; no checks repeated.');return 0
        if not frozen.exists():
            frozen.mkdir()
            for name in report['input_sha256']:
                dest=frozen/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,dest)
        assert all(digest(frozen/n)==h for n,h in report['input_sha256'].items()),'snapshot changed'
        runtime={n:h for n,h in report['input_sha256'].items() if n.startswith(('native/','minidb/')) or n in ('Dockerfile','.dockerignore','requirements.lock')}
        report['source_fingerprint']=fingerprint(runtime);report['harness_fingerprint']=fingerprint(report['input_sha256'])
        dockerfile=(frozen/'Dockerfile').read_text()
        assert not re.search(r'\b(apt(?:-get)?|pip[0-9.]*|curl|wget)\b',dockerfile)
        assert 'ENV MINIDB_POSTINGS=0' in dockerfile
        for name in ('block_scan_impl.h','block_bounds_impl.h'):assert '!native/'+name in (frozen/'.dockerignore').read_text()
        for item in json.loads((frozen/'native/vendor/manifest.json').read_text()):assert digest(frozen/'native/vendor'/item['path'])==item['sha256']
        report['configuration']={'root_dockerfile':runtime['Dockerfile'],'build_network':'none','build_no_cache':True,'entrypoint':['/app/minivectordb-log'],'MINIDB_POSTINGS':'0','runtime_network':'none','cap_drop':'ALL','read_only':True,'swap':'disabled'}
        report['configuration_fingerprint']=fingerprint(report['configuration']);persist()
        run('docker-version',['docker','version'],timeout=60)
        run('build',['docker','build','--platform=linux/amd64','--no-cache','--network=none','--iidfile',out/'image.id','-t','minivectordb:de2-query-'+stamp.lower(),'.'])
        image=(out/'image.id').read_text().strip();assert re.fullmatch(r'sha256:[0-9a-f]{64}',image);report['image']=image
        run('inspect',['docker','image','inspect',image],timeout=60)
        info=json.loads((out/'inspect.log').read_text())[0];assert info['Id']==image
        cfg=info['Config'];assert cfg['Entrypoint']==['/app/minivectordb-log'] and not cfg.get('Cmd')
        assert dict(x.split('=',1) for x in cfg['Env'])['MINIDB_POSTINGS']=='0'
        report['image_config_fingerprint']=fingerprint(cfg)
        probe="import hashlib,json,os; print(json.dumps({'binary_sha256':hashlib.sha256(open('/app/minivectordb-log','rb').read()).hexdigest(),'postings':os.environ.get('MINIDB_POSTINGS')}))"
        run('binary-fingerprint',['docker','run','--rm','--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--entrypoint','python3',image,'-B','-c',probe])
        meta=json.loads((out/'binary-fingerprint.log').read_text());assert meta['postings']=='0';report['binary_sha256']=meta['binary_sha256']
        report['matches_document_binary']=meta['binary_sha256']=='c6b241d868bc79cf38ba26221cec6e1a73d882d3444049ea480f93d6eccc09ba'
        test=out/'test-tools';test.mkdir(exist_ok=True)
        compiler=['docker','run','--rm','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user',f'{os.getuid()}:{os.getgid()}','--tmpfs','/tmp:rw,nosuid,size=64m','--mount',f'type=bind,src={frozen},dst=/src,readonly','--mount',f'type=bind,src={test},dst=/out',BUILDER]
        run('fault-build',compiler+['gcc','-shared','-fPIC','-O2','/src/checks/queryable_fault.c','-ldl','-o','/out/fault.so'])
        run('screen-build',compiler+['gcc','-std=c11','-O3','-shared','-fPIC','-ffp-contract=off','-fno-fast-math','/src/native/screen.c','-lm','-o','/out/screen.so'])
        report['test_tool_sha256']={p.name:digest(p) for p in test.iterdir()}
        # Unit truth runs use the same pinned compiler; this is not a benchmark
        # or a substitute for SQL-level truth on the immutable image.
        unit=['docker','run','--rm','--network=none','--cpus=1','--memory=256m','--memory-swap=256m','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--user',f'{os.getuid()}:{os.getgid()}','--mount',f'type=bind,src={frozen},dst=/src,readonly','--mount',f'type=bind,src={out},dst=/out','--entrypoint','python3',image,'-B','/src/checks/de2_screen_truth.py','--library','/out/test-tools/screen.so','--output','/out/screen-truth']
        run('screen-truth',unit)
        kernel=json.loads((out/'screen-truth/report.json').read_text());report['kernel_truth']=kernel
        require_fast=['--require-fast'] if kernel['status']=='PASS' else []
        focused=[sys.executable,'-B',frozen/'checks/de2_query_validation.py','--image',image,'--fault-library',test/'fault.so']
        regression=[sys.executable,'-B',frozen/'checks/queryable_regression.py','--image',image,'--fault-library',test/'fault.so']
        native=[sys.executable,'-B',frozen/'checks/native_validation.py','--image',image]
        for cpu in ('1','2'):
            name=cpu+'c2g';config=json.loads((frozen/'protocol_check/config.json').read_text());config.update(cpus=cpu,memory='2g');save(out/(name+'.config.json'),config)
            run('public-'+name,[sys.executable,'-B',frozen/'protocol_check/run.py','--image',image,'--config',out/(name+'.config.json'),'--output',out/('public-'+name)])
            run('focused-'+name,focused+['--cpus',cpu,'--memory','2g','--output',out/('focused-'+name)]+require_fast)
        run('contract',[sys.executable,'-B',frozen/'checks/contract.py','--image',image,'--output',out/'contract'])
        run('native',native+['--cpus','1','--memory','2g','--section','all','--output',out/'native'])
        for memory in ('2g','16m'):
            for section in ('faults','transactions'):
                name=section+'-1c'+memory;run(name,regression+['--cpus','1','--memory',memory,'--section',section,'--output',out/name])
        for memory,env in [('12m',[]),('16m',[]),('64m',['MINIDB_INDEX_MMAP=0','MINIDB_SIMD=0']),('2g',DISABLED)]:
            label=memory+('-disabled' if env==DISABLED else '-fallback' if env else '')
            extra=[x for item in env for x in ('--env',item)]
            for section in ('numeric','plans'):
                name=section+'-'+label;run(name,focused+['--cpus','1','--memory',memory,'--section',section,'--output',out/name]+extra)
        run('large-map-32m',focused+['--memory','32m','--section','large','--output',out/'large-map-32m'])
        run('crc-12m',focused+['--memory','12m','--section','crc','--output',out/'crc-12m'])
        assert files()==report['input_sha256'],'source changed during checks'
        report.update(status='PASS',correctness_only=True)
        report['limitations']=['No performance comparison this round.','SIMD=0 verifies scalar fallback; AVX-512 execution is only covered if the recorded CPU/kernel supports it.','No hidden-platform tests or formal submission.']
    except BaseException as e:
        report.update(status='FAILED_OR_BLOCKED',error=repr(e),traceback=traceback.format_exc());print(report['error'],flush=True)
    finally:
        report['conversation_unchanged']=digest(REPO/'agent/conversation.json')==report['conversation_sha256_before'];persist()
        print('REPORT:',out/'report.json');print('STATUS:',report['status']);print('IMAGE:',report.get('image'));print('PERFORMANCE_RUNS:',report['performance_runs'])
    return 0 if report['status']=='PASS' else 1
if __name__=='__main__':raise SystemExit(main())
