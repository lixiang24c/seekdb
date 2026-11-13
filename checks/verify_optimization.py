#!/usr/bin/env python3
"""Freeze and verify the b2 -> layout -> native write -> native query migration.

Default: offline Docker builds, regressions, then 12 measured lifecycles (one per
version/scenario). --host checks correctness only, without resource/gain claims.
All artifacts stay outside the repository; never commit/push or edit conversations.
"""
import argparse,ast,datetime,hashlib,json,math,os,shutil,subprocess,sys,tempfile,time,traceback
from pathlib import Path
HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session
import optimization_resources as resources
from optimization_workload import dataset,workload,check_answers,persistence
BASE='b2ecc6b5afc98f4d8a317106cea7df570a0b69d9'
STAGES=('b2','layout','write','final')
SCENARIOS={'mem12':('1','12m',10000,128,80,'random'),
           'mem64':('1','64m',10000,128,1000,'random'),
           'formal':('2','2g',10000,128,1000,'random')}
RUNTIME=['Dockerfile','.dockerignore','requirements.lock']

def hashes(root):
    files=[root/p for p in RUNTIME]+list((root/'minidb').glob('*.py'))+list((root/'native').glob('*.c'))+list((root/'native').glob('*.cpp'))
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files) if p.exists()}

def harness_hashes():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in (HERE,ROOT/'protocol_check') for p in sorted(folder.rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix in ('.py','.c','.cpp','.json')}

def freeze(out):
    contexts=out/'contexts';contexts.mkdir()
    base=contexts/'b2';base.mkdir()
    archive=subprocess.Popen(['git','archive',BASE],cwd=ROOT,stdout=subprocess.PIPE)
    subprocess.run(['tar','-x','-C',str(base)],stdin=archive.stdout,check=True);archive.stdout.close();assert archive.wait()==0
    layout=contexts/'layout';shutil.copytree(base,layout)
    for name in [*RUNTIME,'minidb/storage.py','minidb/native_storage.py','native/storage.cpp']:
        shutil.copy2(ROOT/name,layout/name)
    p=layout/'minidb/native_storage.py';s=p.read_text();assert s.count('    def available(self):\n')==1
    p.write_text(s.replace('    def available(self):\n','    def available(self):\n        return False  # Frozen layout-only control.\n'))
    write=contexts/'write';shutil.copytree(layout,write)
    for name in ('minidb/native_storage.py','minidb/sql.py'):shutil.copy2(ROOT/name,write/name)
    p=write/'minidb/engine.py';s=p.read_text();current=(ROOT/'minidb/engine.py').read_text()
    insert=current.split('        prepared = self.store.cpp.prepare_insert',1)[1].split('        st = Parser(sql).parse()',1)[0]
    assert s.count('        st = Parser(sql).parse()')==1
    s=s.replace('        st = Parser(sql).parse()','        prepared = self.store.cpp.prepare_insert'+insert+'        st = Parser(sql).parse()')
    assert s.count("convert(table['cols'][i], v)")==1
    p.write_text(s.replace("convert(table['cols'][i], v)","convert(table['cols'][i], v, compact=True)"))
    final=contexts/'final';shutil.copytree(base,final)
    for name in hashes(ROOT):shutil.copy2(ROOT/name,final/name)
    # Freeze current checks separately from runtime images; Docker ignores them.
    shutil.copytree(HERE,out/'checks',ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT/'protocol_check',out/'protocol_check',ignore=shutil.ignore_patterns('__pycache__'))
    return {s:hashes(contexts/s) for s in STAGES}

def extract_probe(name):
    path=HERE/(name+'.py');nodes=[]
    for node in ast.parse(path.read_text()).body:
        nodes.append(node)
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='PROBE' for t in node.targets):break
    else:raise RuntimeError('No probe '+name)
    ns={'__file__':str(path),'__name__':'extract'};exec(compile(ast.Module(body=nodes,type_ignores=[]),str(path),'exec'),ns)
    return ns['PROBE']

def continuation(source, report, out, harness):
    """Carry forward proven checks; recovery fixture blockers remain unverified."""
    source=source.resolve();prior=json.loads((source/'report.json').read_text())
    assert prior['baseline']==BASE and prior['mode']=='Docker isolated full load'
    assert prior['status']=='FAILED_OR_BLOCKED' and not prior['runs']
    assert prior['manifest']==report['manifest'],'Frozen runtime changed; cannot reuse prior evidence'
    assert prior['scenarios']=={k:list(v) for k,v in report['scenarios'].items()}
    changed={k for k in set(harness)|set(prior['harness_sha256']) if harness.get(k)!=prior['harness_sha256'].get(k)}
    assert changed<= {'checks/architecture_recovery.py','checks/verify_optimization.py','checks/storage_fault.c'},changed
    required={stage+'-'+action for stage in STAGES for action in ('build','audit','public')}
    required.update(('architecture-auto','architecture-scalar','architecture-off','contract','model','sanitizer','fault-build'))
    evidence=prior;evidence_path=source/'report.json';chain=[]
    while not required<={c['id'] for c in evidence['checks'] if c['status']=='PASS'}:
        inherited=evidence['inherited_validation'];path=Path(inherited['report'])
        assert str(path) not in chain and len(chain)<8,'Invalid evidence chain'
        assert hashlib.sha256(path.read_bytes()).hexdigest()==inherited['report_sha256']
        evidence=json.loads(path.read_text());chain.append(str(path));evidence_path=path
        assert evidence['manifest']==prior['manifest'] and evidence['images']==prior['images']
    passed={c['id'] for c in evidence['checks'] if c['status']=='PASS'}
    for check in prior['checks']:
        if check['status']!='PASS':
            assert check['id'] in {'native-recovery-12m','native-recovery-64m','native-recovery-2g'}
            memory=check['id'].removeprefix('native-recovery-')
            directory=source/('recovery-'+memory)
            permission_block="Permission denied: '/data'" in (directory/'killFalse.stderr.log').read_text()
            init_signal_block=("AssertionError: ('kill', 99)" in (source/check['stderr']).read_text()
                               and json.loads((directory/'report.json').read_text())['preflight']['status']=='PASS')
            assert permission_block or init_signal_block,'Unrecognized recovery failure; do not bypass it'
    for stage in STAGES:
        assert hashes(source/'contexts'/stage)==prior['manifest'][stage]
        assert prior['images'][stage].startswith('sha256:') and len(prior['images'][stage])==71
    report['images']=prior['images'].copy()
    report['inherited_validation']={'report':str(source/'report.json'),
        'report_sha256':hashlib.sha256((source/'report.json').read_bytes()).hexdigest(),
        'validated_checks_report':str(evidence_path),'evidence_chain':chain,
        'passed_checks':sorted(passed),'changed_harness_files':sorted(changed),
        'previous_block':'Recovery fixture directory permissions or PID 1 self-signal. Recovery and performance unverified.'}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',action='store_true');p.add_argument('--output',type=Path);p.add_argument('--resume',type=Path)
    p.add_argument('--continue-from',type=Path,help='Reuse images and proven checks from a fixture-blocked report; rebuild the test-only fault shim, run recovery, then original workloads.')
    p.add_argument('--scenarios',nargs='+',choices=SCENARIOS,default=list(SCENARIOS))
    p.add_argument('--image',action='append',default=[],metavar='STAGE=IMAGE',help='Reuse audited images; missing stages are built once.')
    a=p.parse_args();stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    if a.continue_from and (a.host or a.resume or a.image):p.error('--continue-from cannot combine with --host, --resume or --image')
    out=(a.resume or a.output or ROOT.parent/'reports'/('architecture-'+('host-' if a.host else 'docker-')+stamp)).resolve();out.mkdir(parents=True,exist_ok=True)
    before=hashes(ROOT);harness=harness_hashes();conversation=hashlib.sha256((ROOT/'agent/conversation.json').read_bytes()).hexdigest()
    report={'status':'RUNNING','baseline':BASE,'working_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
      'mode':'host correctness only' if a.host else 'Docker isolated full load','manifest':{},'harness_sha256':harness,'images':{},'checks':[],'runs':[],
      'scenarios':{k:SCENARIOS[k] for k in a.scenarios},'rounds_per_version_scenario':1,
      'additional_queries':{'first_touch':20,'warmup':20,'accounting':'Both phases retained separately and included in full_stage_sum/full I/O/memory.'},
      'stage_meaning':{'layout':'b2 + precompiled Python, scalar/vector separation, Python column merges and budgeted preparation',
        'write':'layout + C++ storage, vector conversion and bulk INSERT',
        'final':'write + scalar filtering, mask trimming, VA/page reads and exact native global TopK'},
      'comparisons':[['b2','layout'],['layout','write'],['write','final'],['b2','final']],
      'limitations':['One run per version/scenario, descriptive evidence only; no stable gain inferred.',
        'Document intermediate source revisions and original verify_optimization.py were not supplied. Stages here are dependency groups, not identical historical single-feature controls.',
        'Document short-query count/seed not specified: mem12 uses 80, others 1000; 50% filtered, K=10, batch=100, body=512 bytes, continuous float32.',
        'No global cache reset; new volume/cgroup per lifecycle. Startup/preparation/first queries retained. No profiler in timed runs.',
        'I/O is cgroup data-device attribution; unavailable attribution remains unknown. Memory peaks include page cache and are cumulative per boundary.',
        'Platform hidden benchmark and score mapping remain unknown; phase speedups are never multiplied.']}
    def save(): (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    def run(name,argv,*,cwd=None,env=None,source=None,required=True,timeout=600):
        folder=out/'logs';folder.mkdir(exist_ok=True)
        print('['+name+']',flush=True);(folder/(name+'.command.json')).write_text(json.dumps(argv,ensure_ascii=False))
        try:
            with (folder/(name+'.stdout')).open('wb') as stdout,(folder/(name+'.stderr')).open('wb') as stderr:
                result=subprocess.run(argv,cwd=cwd or ROOT,env=env,input=source.encode() if source is not None else None,stdout=stdout,stderr=stderr,timeout=timeout)
            status='PASS' if result.returncode==0 else 'FAIL';code=result.returncode
            if status=='FAIL' and name.startswith('native-recovery-'):
                recovery_report=out/('recovery-'+name.removeprefix('native-recovery-'))/'report.json'
                if recovery_report.exists() and json.loads(recovery_report.read_text()).get('status','').startswith('BLOCKED'):
                    status='BLOCKED'
        except subprocess.TimeoutExpired:status='TIMEOUT';code=None
        report['checks'].append({'id':name,'status':status,'exit_code':code,'stdout':'logs/'+name+'.stdout','stderr':'logs/'+name+'.stderr'});save()
        if required and status!='PASS':raise RuntimeError(name+' '+status+'; see '+str(folder/(name+'.stderr')))
        return (folder/(name+'.stdout')).read_text()
    def probe_command(image,extra=()):
        if a.host:return [sys.executable,'-B','-']
        return ['docker','run','--rm','-i','--network=none','--cpus=2','--memory=2g','--memory-swap=2g','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges','--pids-limit=128',
          '--tmpfs','/data:rw,size=64m','--tmpfs','/tmp:rw,size=64m','-e','PYTHONPATH=/app',*extra,'--entrypoint=python3',image,'-B','-']
    try:
        # Clear inherited diagnostic switches so benchmarks use default production paths.
        for key in list(os.environ):
            if key.startswith('MINIDB_'):del os.environ[key]
        if a.resume:
            report=json.loads((out/'report.json').read_text());assert report['scenarios']=={k:list(SCENARIOS[k]) for k in a.scenarios}
            assert (report['mode']=='host correctness only')==a.host
            assert report['manifest']['final']==before,'Current runtime differs from frozen candidate'
            assert report['harness_sha256']==harness,'Harness changed; start a fresh output directory'
            for stage in STAGES:assert hashes(out/'contexts'/stage)==report['manifest'][stage]
            report['status']='RUNNING';report.pop('error',None)
            report.setdefault('previous_checks',[]).append(report['checks']);report['checks']=[]
        else:report['manifest']=freeze(out)
        contexts=out/'contexts'
        if a.continue_from:continuation(a.continue_from,report,out,harness)
        save()
        if not a.host:run('docker-version',['docker','version'])
        for spec in a.image:
            stage,image=spec.split('=',1);assert stage in STAGES;report['images'][stage]=image
        if not a.continue_from:
            for stage in STAGES:
                context=contexts/stage
                if a.host:
                    run(stage+'-build-c',['gcc','-std=c11','-O3','-fPIC','-shared','-ffp-contract=off','-fno-fast-math','native/screen.c','-o','minidb/libscreen.so','-lm'],cwd=context)
                    if stage!='b2':run(stage+'-build-cpp',['g++','-std=c++17','-O3','-fPIC','-shared','-static-libstdc++','-static-libgcc','-ffp-contract=off','-fno-fast-math','native/storage.cpp','-Lminidb','-lscreen',"-Wl,-rpath,$ORIGIN",'-o','minidb/libstorage.so'],cwd=context)
                else:
                    if stage not in report['images']:
                        run(stage+'-build',['docker','build','--platform=linux/amd64','--no-cache','--network=none','--iidfile',str(out/(stage+'.iid')),'.'],cwd=context,timeout=1200)
                        report['images'][stage]=(out/(stage+'.iid')).read_text().strip();save()
                    audit="import hashlib,json,pathlib;from minidb import native; p=pathlib.Path('/app');assert native.available(); print(json.dumps({str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in (p/'minidb').glob('*.py')}))"
                    actual=json.loads(run(stage+'-audit',['docker','run','--rm','--network=none','--entrypoint=python3',report['images'][stage],'-B','-c',audit]))
                    assert actual=={k:v for k,v in report['manifest'][stage].items() if k.startswith('minidb/') and k.endswith('.py')},'Image source mismatch'
                env={**os.environ,'PYTHONPATH':str(context),'PYTHONDONTWRITEBYTECODE':'1'}
                transport=['--command',json.dumps([sys.executable,'-B','-u','-m','minidb.server','{data_dir}'])] if a.host else ['--image',report['images'][stage]]
                run(stage+'-public',[sys.executable,'-B',str(ROOT/'protocol_check/run.py'),*transport,'--output',str(out/'public'/stage)],env=env,cwd=context)
            # Focused regressions on the final candidate, plus actual native path counts.
            context=contexts/'final';env={**os.environ,'PYTHONPATH':str(context),'PYTHONDONTWRITEBYTECODE':'1'};image=report['images'].get('final')
            for name in ['vector_text','vector_codec','parser_scan','projection_distances','native_screen','bitmap_boundaries','combined_overlays','graph_boundaries','runtime_native']:
                source=extract_probe(name)
                if name=='combined_overlays':source=source.replace('import heapq,json,math','import heapq,json,math\nfrom minidb import indexio\nindexio.file_cache_budget=lambda: 64 << 20')
                for backend in (['auto','scalar'] if name in ('native_screen','runtime_native','combined_overlays') else ['auto']):
                    settings={'MINIDB_SIMD':'0' if backend=='scalar' else '1','MINIDB_ANN_GRAPH':'1' if name=='graph_boundaries' else '0'}
                    extra=sum((['-e',k+'='+v] for k,v in settings.items()),[])
                    with tempfile.TemporaryDirectory() as data:
                        run(name+'-'+backend,probe_command(image,extra),source=source.replace("'/data'",repr(data)) if a.host else source,env={**env,**settings},cwd=context)
            # A mounted baseline is used for independent old-parser/state comparisons.
            source=(HERE/'architecture_probe.py').read_text().replace("sys.path.insert(0, str(Path(__file__).resolve().parents[1]))",'')
            for backend in ('auto','scalar','off'):
                args=['--baseline',str(contexts/'b2') if a.host else '/baseline']
                if backend=='off':args+=['--section','database']
                prefix='sys.argv='+repr(['architecture_probe.py',*args])+'\n'
                source_run='import sys\n'+prefix+source
                extra=['-v',str(contexts/'b2')+':/baseline:ro','-e','MINIDB_SIMD='+('0' if backend=='scalar' else '1')]
                extra+=['-e','MINIDB_NATIVE='+('0' if backend=='off' else '1')]
                run('architecture-'+backend,probe_command(image,extra),source=source_run,env={**env,'MINIDB_SIMD':'0' if backend=='scalar' else '1','MINIDB_NATIVE':'0' if backend=='off' else '1'},cwd=context)
            # Standalone legacy fixtures use real processes. Native kill tests follow separately.
            if a.host:
                for name in ('overlay_boundaries','overlay_recovery','general_boundaries','recovery','merge_recovery','parser_fastpaths'):
                    shutil.copy2(HERE/(name+'.py'),context/'checks'/(name+'.py'))
                    run(name,[sys.executable,'-B',str(context/'checks'/(name+'.py'))],cwd=context,env=env)
            else:
                run('parser-fastpaths',[sys.executable,'-B',str(HERE/'parser_fastpaths.py'),'--image',image])
            # Contract/state-machine transport supports host adaptation without production hooks.
            adapter="import runpy,sys;sys.path.insert(0,sys.argv[1]);import _transport;original=_transport.Session\ndef session(*a,**kw):\n kw.pop('image',None);kw['command']=[sys.executable,'-B','-u','-m','minidb.server','{data_dir}'];return original(*a,**kw)\n_transport.Session=session\nscript=sys.argv[2];sys.argv=sys.argv[2:];runpy.run_path(script,run_name='__main__')"
            for name,options in [('contract',[]),('model',['--dimensions','128']),('native_topk_sql',[])]:
                cmd=[sys.executable,'-B',str(HERE/(name+'.py')),'--image',image or 'HOST','--output',str(out/name),*options]
                if a.host:cmd=[sys.executable,'-B','-c',adapter,str(ROOT/'protocol_check'),*cmd[2:]]
                run(name,cmd,cwd=context,env=env)
            # Sanitizers run separately from every performance lifecycle.
            san=out/'sanitizer';san.mkdir(exist_ok=True);(san/'data').mkdir(exist_ok=True)
            builder=(ROOT/'Dockerfile').read_text().splitlines()[0].split()[1]
            prefix=[] if a.host else ['docker','run','--rm','--network=none','-v',str(ROOT)+':/src:ro','-v',str(san)+':/work','-w','/src',builder]
            source_root=str(ROOT) if a.host else '/src';work=str(san) if a.host else '/work'
            flags=['-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-ffp-contract=off','-fno-fast-math']
            run('sanitizer-c',prefix+['gcc','-std=c11',*flags,'-c',source_root+'/native/screen.c','-o',work+'/screen.o'])
            run('sanitizer-cpp',prefix+['g++','-std=c++17',*flags,source_root+'/checks/storage_sanitizer.cpp',work+'/screen.o','-lm','-o',work+'/check'])
            run('sanitizer',prefix+['env','ASAN_OPTIONS=detect_leaks=0','UBSAN_OPTIONS=halt_on_error=1',work+'/check',work+'/data'])
            report['sanitizer_scope']='ASan memory access and UBSan; leak detection disabled, no leak-free claim.'
            shim=out/'fault.so'
            if a.host:run('fault-build',['gcc','-O2','-fPIC','-shared',str(HERE/'storage_fault.c'),'-o',str(shim),'-ldl'])
            else:
                builder=(ROOT/'Dockerfile').read_text().splitlines()[0].split()[1]
                run('fault-build',['docker','run','--rm','--network=none','-v',str(HERE)+':/checks:ro','-v',str(out)+':/out',builder,'gcc','-O2','-fPIC','-shared','/checks/storage_fault.c','-o','/out/fault.so','-ldl'])
        else:
            context=contexts/'final';env={**os.environ,'PYTHONPATH':str(context),'PYTHONDONTWRITEBYTECODE':'1'}
            image=report['images']['final'];shim=out/'fault.so'
            for stage in STAGES:
                actual=run(stage+'-reuse-image',['docker','image','inspect',report['images'][stage],'--format','{{.Id}}']).strip()
                assert actual==report['images'][stage],'Immutable image ID mismatch'
            # Runtime images remain unchanged. Only the externally mounted test
            # shim is rebuilt, using the same offline pinned compiler image.
            builder=(ROOT/'Dockerfile').read_text().splitlines()[0].split()[1]
            run('fault-build-updated',['docker','run','--rm','--network=none','--cap-drop=ALL',
                '--security-opt=no-new-privileges','--user',f'{os.getuid()}:{os.getgid()}',
                '--read-only','--tmpfs','/tmp:rw,size=32m','-v',str(HERE)+':/checks:ro',
                '-v',str(out)+':/out',builder,'gcc','-O2','-fPIC','-shared',
                '/checks/storage_fault.c','-o','/out/fault.so','-ldl'])
        report['fault_shim_sha256']=hashlib.sha256(shim.read_bytes()).hexdigest();save()
        for memory in (['host'] if a.host else ['12m','64m','2g']):
            run('native-recovery-'+memory,[sys.executable,'-B',str(HERE/'architecture_recovery.py'),'--shim',str(shim),'--output',str(out/('recovery-'+memory)),*(['--image',image,'--memory',memory] if not a.host else [])],env=env,cwd=context,required=False)
        recovery_checks=[c for c in report['checks'] if c['id'].startswith('native-recovery-')]
        assert recovery_checks and all(c['status']=='PASS' for c in recovery_checks),'Recovery checks incomplete; performance workloads were not started'
        if not a.host:
            resources.HERE=HERE;resources.OUT=out
            cfg=json.loads((ROOT/'protocol_check/config.json').read_text())
            for scenario_index,name in enumerate(a.scenarios):
                cpu,memory,n,dim,nq,distribution=SCENARIOS[name]
                # Reuse the exact same independent dataset for all four versions.
                data_dir=out/'datasets'/name
                print(f'[oracle] {name}: {n} rows, {dim} dimensions, {nq} queries; outside timing',flush=True)
                vectors,inserts,statements,truth=dataset(data_dir,n,dim,nq,distribution,20261113)
                order=STAGES if scenario_index%2==0 else tuple(reversed(STAGES))
                for stage in order:
                    if any(x['scenario']==name and x['variant']==stage for x in report['runs']):continue
                    tag=name+'-'+stage;print('[trial] '+tag,flush=True)
                    resources.CONFIG={**cfg,'cpus':cpu,'memory':memory};resources.MEMORY_LIMIT=int(memory[:-1])*(1<<30 if memory.endswith('g') else 1<<20);resources.IMAGE=report['images'][stage]
                    s=Session(resources.CONFIG,out/(tag+'.stderr.log'),image=resources.IMAGE,case_timeout=3600)
                    sample={'scenario':name,'variant':stage,'round':1,'image':resources.IMAGE,'container':s.name,'volume':s.volume,'dataset':str(data_dir.relative_to(out)),'active_phase':'startup'};observer=None;success=False
                    try:
                        start=time.perf_counter();s.start();startup=time.perf_counter()-start;observer=resources.Observer(s)
                        inspected=json.loads((out/(s.name+'.inspect.json')).read_text());assert inspected['HostConfig']['NetworkMode']=='none'
                        answers,first=workload(s,observer,{'dim':dim},inserts,statements,['unfiltered','filtered'] if scenario_index%2==0 else ['filtered','unfiltered'],sample)
                        sample['seconds']['startup']=startup;sample['seconds']['full_stage_sum']=sum(v for k,v in sample['seconds'].items() if k!='query')
                        end=sample['resources']['final_writeback_settled'];warm=sample['resources']['after_warmup'];device=end['data_device']
                        sample['actual_index_bytes']=sample['resources']['after_index']['index_file_bytes']
                        assert sample['actual_index_bytes']>=n*(8+dim*4)
                        visible=sample['resources']['after_write']['io_by_device'].get(device,{}).get('wbytes',0)>0
                        sample['full_io_bytes']=resources.data_bytes(end) if visible else None;sample['query_io_bytes']=resources.data_bytes(end)-resources.data_bytes(warm) if visible else None;sample['memory_peak_bytes']=end['memory_peak_bytes']
                        previous_io=previous_cpu=0;sample['phase_resources']={}
                        for phase,snapshot in sample['resources'].items():
                            counters=snapshot['io_by_device'].get(device,{})
                            current_io=counters.get('rbytes',0)+counters.get('wbytes',0)
                            current_cpu=snapshot.get('cpu_stat',{}).get('usage_usec')
                            sample['phase_resources'][phase]={
                                'io_bytes_since_previous_boundary':current_io-previous_io if visible else None,
                                'cpu_usec_since_previous_boundary':current_cpu-previous_cpu if current_cpu is not None and previous_cpu is not None else None,
                                'memory_peak_since_start_bytes':snapshot['memory_peak_bytes'],
                                'memory_stat':snapshot['memory_stat']}
                            previous_io,previous_cpu=current_io,current_cpu
                        sample['tails']={group:{p:sorted(values)[max(0,math.ceil(q*len(values))-1)] for p,q in [('p95',.95),('p99',.99),('max',1.)]} for group,values in sample['latencies_seconds'].items()}
                        assert end['memory_events'].get('oom_kill',0)==0
                        observer.close();observer=None
                        check_answers(answers,truth);check_answers(first,truth[:20]*2)
                        encoded=json.dumps(answers,separators=(',',':'));(out/(tag+'.answers.json')).write_text(encoded);sample['output_sha256']=hashlib.sha256(encoded.encode()).hexdigest()
                        sample['active_phase']='restart_and_transaction_regression';persistence(s,vectors)
                        sample['status']='PASS' if visible else 'UNKNOWN_IO';success=True
                    except Exception as e:
                        sample.update(status='FAIL',error=str(e));(out/(tag+'.error.log')).write_text(traceback.format_exc())
                        inspection=subprocess.run(['docker','inspect',s.name],capture_output=True,text=True);(out/(tag+'.failed-inspect.json')).write_text(inspection.stdout+inspection.stderr)
                        try:
                            if json.loads(inspection.stdout)[0]['State'].get('OOMKilled'):sample['status']='OOM'
                        except (ValueError,KeyError,IndexError):pass
                        if observer:
                            try:sample['failure_resources']=observer.snapshot()
                            except Exception as e:sample['failure_observer_error']=str(e)
                    finally:
                        if observer:observer.close()
                        if not success:s.volume_created=False;report.setdefault('retained_failure_volumes',[]).append(s.volume)
                        s.close();report['runs'].append(sample);save();print(tag,sample['status'],flush=True)
            report['pairwise']=summarize(report)
            complete=len(report['runs'])==len(a.scenarios)*len(STAGES)
            report['status']='PASS' if complete and all(x['status']=='PASS' for x in report['runs']+report['checks']) else 'COMPLETED_WITH_FAILURES_OR_UNSUPPORTED_LIMITS'
        else:report['status']='HOST_PASS_DOCKER_PENDING'
    except BaseException as e:
        report.update(status='FAILED_OR_BLOCKED',error=str(e));(out/'error.log').write_text(traceback.format_exc());print(traceback.format_exc(),flush=True)
    finally:
        report['runtime_unchanged_during_validation']=hashes(ROOT)==before
        report['harness_unchanged_during_validation']=harness_hashes()==harness
        report['conversation_unchanged']=hashlib.sha256((ROOT/'agent/conversation.json').read_bytes()).hexdigest()==conversation
        save();print('REPORT:',out/'report.json');print('STATUS:',report['status']);print('COMPLETED_RUNS:',len(report['runs']))
    return int(report['status']=='FAILED_OR_BLOCKED')

def summarize(report):
    result=[]
    for scenario in report['scenarios']:
        runs={r['variant']:r for r in report['runs'] if r['scenario']==scenario}
        for old,new in report['comparisons']:
            a,b=runs.get(old),runs.get(new)
            item={'scenario':scenario,'baseline':old,'candidate':new,'baseline_status':a and a['status'],'candidate_status':b and b['status']}
            if a and b and a['status'] in ('PASS','UNKNOWN_IO') and b['status'] in ('PASS','UNKNOWN_IO'):
                assert a['output_sha256']==b['output_sha256'],'Version full-output mismatch'
                metrics={k:(a['seconds'][k],b['seconds'][k]) for k in a['seconds']}
                metrics.update({k:(a[k],b[k]) for k in ('full_io_bytes','query_io_bytes','memory_peak_bytes')})
                item['metrics']={k:{'baseline':x,'candidate':y,'change_percent':(y/x-1)*100 if x and y is not None else None} for k,(x,y) in metrics.items()}
                item['tails']={g:{p:{'baseline':a['tails'][g][p],'candidate':b['tails'][g][p]} for p in ('p95','p99','max')} for g in a['tails']}
            result.append(item)
    return result
if __name__=='__main__':raise SystemExit(main())
