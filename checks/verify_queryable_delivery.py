#!/usr/bin/env python3
"""Build root Dockerfile once, verify that immutable default image, then freeze.

No benchmarks, optimization changes, commits, pushes, or formal submissions.
Public/recovery/transaction checks: one pass each at 1c2g, 2c2g, 1c16m.
Default-off and positive-control-on path probes use that same binary/image.
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
import tarfile
import traceback

from verify_queryable import REPO, BUILDER, files, digest, save


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path);parser.add_argument('--resume',type=Path)
    parser.add_argument('--plan',action='store_true');args=parser.parse_args()
    if args.plan:print(__doc__);return 0
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=(args.resume.parent if args.resume else args.output or REPO.parent/'reports'/('queryable-delivery-'+stamp)).resolve()
    accepted_path=REPO/'QUERYABLE_LOG_MIGRATION_RESULT.json'
    if args.resume:report=json.loads(args.resume.read_text())
    else:
        out.mkdir(parents=True,exist_ok=False)
        report={'status':'STARTED','head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
                'branch':subprocess.check_output(['git','branch','--show-current'],cwd=REPO,text=True).strip(),
                'input_sha256':files(),'acceptance_sha256':digest(accepted_path),
                'running_instructions_sha256_before':digest(REPO/'RUNNING.txt'),
                'conversation_sha256_before':digest(REPO/'agent/conversation.json'),
                'checks':{},'image':None,'frozen':False,'performance_runs':0,
                'known_limitations':['Index-build regression, individual slower rounds, and observed latency maxima remain in the previous acceptance report.',
                                    'Large-data stress peak memory/I/O were not measured. This delivery gate adds no new performance claim.',
                                    'A source archive and image ID are retained; the Docker image itself is not exported.']}
    frozen=out/'source'
    def persist():save(out/'report.json',report)
    def assert_inputs():
        assert files()==report['input_sha256'],'source/configuration/harness changed; start a new delivery'
        assert digest(accepted_path)==report['acceptance_sha256'],'acceptance record changed'
    def run(name,argv,cwd=None,timeout=3600):
        if report['checks'].get(name,{}).get('status')=='PASS':return
        words=list(map(str,argv));log=out/(name+'.log')
        if '--output' in words:
            previous=Path(words[words.index('--output')+1])
            if previous.exists():
                assert previous.parent==out
                history=out/'retry-history';history.mkdir(exist_ok=True);previous.rename(history/(name+'-'+stamp))
        if log.exists():shutil.copyfile(log,out/(name+'-'+stamp+'.previous.log'))
        report['active']=name;persist();save(out/(name+'.command.json'),words);print('CHECK',name,flush=True)
        with log.open('wb') as stream:
            result=subprocess.run(words,cwd=cwd or frozen,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
        report['checks'][name]={'status':'PASS' if result.returncode==0 else 'FAIL','returncode':result.returncode,'log':log.name};persist()
        if result.returncode:raise RuntimeError(name+': '+log.read_text(errors='replace')[-3000:])
    try:
        assert_inputs()
        if report['status']=='PASS':
            assert report['frozen'] and digest(REPO/'RUNNING.txt')==report['running_instructions_sha256']
            assert digest(out/'frozen-source.tar.gz')==report['frozen_archive_sha256']
            print('Already frozen; no checks repeated.');return 0
        report['status']='STARTED';report.pop('error',None);report.pop('traceback',None)
        accepted=json.loads(accepted_path.read_text())
        for name,expected in accepted['accepted_source_and_harness_sha256'].items():
            if name.startswith(('native/','minidb/')):assert digest(REPO/name)==expected,('runtime changed after acceptance',name)
        if not frozen.exists():
            frozen.mkdir()
            for name in report['input_sha256']:
                dest=frozen/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/name,dest)
        assert all(digest(frozen/n)==h for n,h in report['input_sha256'].items()),'frozen input modified'
        runtime={n:h for n,h in report['input_sha256'].items() if n.startswith(('native/','minidb/')) or n in ('Dockerfile','.dockerignore','requirements.lock')}
        report['source_fingerprint']=fingerprint(runtime)
        report['configuration']={'build_files':{n:runtime[n] for n in ('Dockerfile','.dockerignore','requirements.lock')},
                                 'entrypoint':['/app/minivectordb-log'],'environment':{'MINIDB_POSTINGS':'0'},
                                 'build_network':'none','build_no_cache':True,'platform':'linux/amd64'}
        report['configuration_fingerprint']=fingerprint(report['configuration'])
        for item in json.loads((frozen/'native/vendor/manifest.json').read_text()):assert digest(frozen/'native/vendor'/item['path'])==item['sha256']
        dockerfile=(frozen/'Dockerfile').read_text()
        assert dockerfile.count('ENV MINIDB_POSTINGS=0')==1 and 'ENTRYPOINT ["/app/minivectordb-log"]' in dockerfile
        assert not re.search(r'\b(apt(?:-get)?|pip[0-9.]*|curl|wget)\b',dockerfile), 'unexpected download/install instruction'
        save(out/'accepted-performance-reference.json',accepted);persist()
        run('docker-version',['docker','version'],timeout=60)
        run('build',['docker','build','--platform=linux/amd64','--no-cache','--network=none','-f','Dockerfile','--iidfile',out/'image.id','-t','minivectordb:delivery-'+out.name.lower(),'.'])
        image=(out/'image.id').read_text().strip();assert re.fullmatch(r'sha256:[0-9a-f]{64}',image);report['image']=image
        run('inspect',['docker','image','inspect',image],timeout=60)
        inspected=json.loads((out/'inspect.log').read_text())[0];assert inspected['Id']==image
        config=inspected['Config'];assert config['Entrypoint']==['/app/minivectordb-log'] and not config.get('Cmd')
        env=dict(item.split('=',1) for item in config['Env']);assert env['MINIDB_POSTINGS']=='0'
        report['image_config']=config;report['image_config_fingerprint']=fingerprint(config)
        probe="import hashlib,json,os;print(json.dumps({'binary_sha256':hashlib.sha256(open('/app/minivectordb-log','rb').read()).hexdigest(),'postings':os.environ.get('MINIDB_POSTINGS')}))"
        run('binary-fingerprint',['docker','run','--rm','--network=none','--cpus=1','--memory=128m','--memory-swap=128m','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--entrypoint','python3',image,'-B','-c',probe])
        binary=json.loads((out/'binary-fingerprint.log').read_text());assert binary['postings']=='0';report['binary_sha256']=binary['binary_sha256'];persist()
        fault=out/'test-tools';fault.mkdir(exist_ok=True)
        run('fault-build',['docker','run','--rm','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--user',f'{os.getuid()}:{os.getgid()}','--tmpfs','/tmp:rw,nosuid,size=64m','--mount',f'type=bind,src={frozen},dst=/src,readonly','--mount',f'type=bind,src={fault},dst=/out',
            BUILDER,'gcc','-shared','-fPIC','-O2','/src/checks/queryable_fault.c','-ldl','-o','/out/fault.so'])
        report['fault_library_sha256']=digest(fault/'fault.so')
        gate=[sys.executable,'-B',frozen/'checks/queryable_regression.py','--image',image,'--fault-library',fault/'fault.so']
        # No MINIDB_POSTINGS environment override on the default probe.
        run('default-paths',gate+['--section','paths','--cpus','1','--memory','2g','--expected-postings','0','--output',out/'default-paths'])
        run('postings-positive-control',gate+['--section','paths','--cpus','1','--memory','2g','--env','MINIDB_POSTINGS=1','--expected-postings','1','--output',out/'postings-positive-control'])
        report['default_path_diagnostics']=json.loads((out/'default-paths/report.json').read_text())['checks']['paths']['diagnostics']
        report['positive_control_diagnostics']=json.loads((out/'postings-positive-control/report.json').read_text())['checks']['paths']['diagnostics']
        assert report['default_path_diagnostics']['posting_scans']==0 and report['positive_control_diagnostics']['posting_scans']>0
        for cpu,memory in [('1','2g'),('2','2g'),('1','16m')]:
            label=cpu+'c'+memory;cfg=json.loads((frozen/'protocol_check/config.json').read_text());cfg.update(cpus=cpu,memory=memory);save(out/(label+'.config.json'),cfg)
            run('public-'+label,[sys.executable,'-B',frozen/'protocol_check/run.py','--image',image,'--config',out/(label+'.config.json'),'--output',out/('public-'+label)])
            for section in ('faults','transactions'):
                run(section+'-'+label,gate+['--section',section,'--cpus',cpu,'--memory',memory,'--output',out/(section+'-'+label)])
        assert_inputs()
        assert digest(REPO/'RUNNING.txt') in (report['running_instructions_sha256_before'],report.get('running_instructions_sha256')),'RUNNING.txt changed; do not overwrite concurrent edits'
        report['public_cases']=sum(len(json.loads((out/('public-'+label)/'result.json').read_text())['cases']) for label in ('1c2g','2c2g','1c16m'))
        report['recovery_groups']=sum(len(json.loads((out/('faults-'+label)/'report.json').read_text())['checks']['faults']) for label in ('1c2g','2c2g','1c16m'))
        report['transaction_profiles']=3
        # Update only delivery documentation; historical measurements stay intact.
        instructions=(REPO/'RUNNING.txt').read_text();start=instructions.index('已验收的原生镜像：') if '已验收的原生镜像：' in instructions else instructions.index('最终交付镜像：')
        instructions=instructions[:start]+f'''最终交付镜像：
{image}
由根 Dockerfile 断网、无缓存构建。默认入口 /app/minivectordb-log；
镜像自身默认 MINIDB_POSTINGS=0，无需额外覆盖。默认关闭及显式开启的路径计数已核验。
源码指纹：{report['source_fingerprint']}
配置指纹：{report['configuration_fingerprint']}
二进制 SHA256：{report['binary_sha256']}
本次交付报告：{out/'report.json'}
三档资源（1c2g、2c2g、1c16m）共 60 个公开用例、75 组恢复检查及事务检查通过。
同一最终镜像完成这些检查；未重跑性能，不扩大历史性能结论。
建索引回退、个别轮次变慢、已知长尾和压力场景峰值/I/O缺失继续保留。
实验 Dockerfile 仅供历史消融复核，生产构建使用根 Dockerfile。

版本冻结。未提交、未推送、未正式提测。
'''
        (out/'RUNNING.final.txt').write_text(instructions)
        report['running_instructions_sha256']=digest(out/'RUNNING.final.txt')
        with tarfile.open(out/'frozen-source.tar.gz','w:gz') as archive:
            for name in sorted(report['input_sha256']):archive.add(frozen/name,arcname=name)
            archive.add(out/'RUNNING.final.txt',arcname='RUNNING.txt')
        report['frozen_archive_sha256']=digest(out/'frozen-source.tar.gz')
        assert_inputs();report['status']='FINALIZING';persist()
        temporary=REPO/'RUNNING.txt.delivery.tmp';temporary.write_text(instructions);temporary.replace(REPO/'RUNNING.txt')
        report.update(status='PASS',frozen=True,conversation_unchanged=digest(REPO/'agent/conversation.json')==report['conversation_sha256_before'],
                      committed=False,pushed=False,formal_submission=False)
        persist();save(REPO/'QUERYABLE_LOG_DELIVERY.json',report)
    except BaseException as error:
        report.update(status='FAILED_OR_BLOCKED',frozen=False,error=repr(error),traceback=traceback.format_exc());print(report['error'],flush=True)
    finally:
        report['conversation_unchanged']=digest(REPO/'agent/conversation.json')==report['conversation_sha256_before']
        persist();print('REPORT:',out/'report.json');print('STATUS:',report['status']);print('IMAGE:',report.get('image'));print('FROZEN:',report['frozen'])
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
