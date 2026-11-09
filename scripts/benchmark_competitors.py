#!/usr/bin/env python3
"""Same-workload diagnostic on the common SQL subset, not an admission score."""
import argparse
import contextlib
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np
from evaluation.benchmark import prepare,vector_sql
from evaluation.driver import Client
from evaluation.stress_run import Process,decode_text

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ours',default='minivectordb:team')
parser.add_argument('--teammate',default='minivectordb:teammate-e5af6d16')
parser.add_argument('--rows',type=int,default=10000);parser.add_argument('--queries',type=int,default=1000)
parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--distribution',choices=('normal','uniform'),default='uniform')
parser.add_argument('--seed',type=int)
parser.add_argument('--verify-memory',default='128m',help='Post-measurement persistence verifier container memory')
parser.add_argument('--resume',action='store_true')
parser.add_argument('--reuse-write-data',action='store_true',help='Query cold-reopens persisted writer data instead of loading it again')
args=parser.parse_args();args.output=args.output.resolve();args.output.mkdir(parents=True,exist_ok=args.resume)
report={'scope':'common-subset diagnostic: bare vector literals, SELECT id TopK; not official admission or score',
        'configuration':{'rows':args.rows,'dimensions':128,'queries':args.queries,'rounds':args.rounds,
                         'cpus':2,'memory_bytes':128*1024*1024,'swap':False,'warmup_queries':10},
        'resource_scope':'whole database container cgroup; all I/O devices; host oracle excluded',
        'images':{},'rounds':[],'status':'RUNNING'}
report['configuration'].update(distribution=args.distribution,fixed_seed=args.seed,verification_memory=args.verify_memory)
if args.resume:
    previous=json.loads((args.output/'report.json').read_text())
    if previous['configuration']['rows']!=args.rows or previous['configuration']['queries']!=args.queries:raise ValueError('resume workload mismatch')
    previous.setdefault('resume_history',[]).append({'prior_status':previous['status'],'prior_error':previous.pop('error',None)})
    report=previous;report['status']='RUNNING'
report['configuration']['reuse_writer_data']=args.reuse_write_data
if args.reuse_write_data:report['resource_scope']='write: startup and load; query: cold reopen, index preparation, warmup and queries; whole cgroup, all I/O devices'
for version,image in (('ours',args.ours),('teammate',args.teammate)):
    identity={'tag':image,'id':subprocess.check_output(['docker','image','inspect','--format','{{.Id}}',image],text=True).strip()}
    if args.resume and report['images'].get(version)!=identity:raise ValueError('resume image mismatch')
    report['images'][version]=identity
def save():
    (args.output/'report.json').write_text(json.dumps(report,indent=2)+'\n')

@contextlib.contextmanager
def session(version,number,track,data_override=None):
    directory=args.output/version/str(number)/track;directory.mkdir(parents=True,exist_ok=args.resume)
    data=directory/'data' if data_override is None else data_override
    data.mkdir(exist_ok=True);name='minivector-common-'+uuid.uuid4().hex
    memory=args.verify_memory if track=='write-verify' else '128m'
    command=['docker','run','--rm','-i','--name',name,'--platform','linux/amd64','--network','none',
             '--cpus','2','--memory',memory,'--memory-swap',memory,'-v',str(data)+':/data',report['images'][version]['tag']]
    with (directory/'stderr.log').open('w') as log:
        process=Process(command,data,log,'json' if version=='ours' else 'text',timeout=60)
        try:
            yield process,name
        finally:
            try:process.stop()
            finally:subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

def metrics(name):
    pid=int(subprocess.check_output(['docker','inspect','-f','{{.State.Pid}}',name],text=True))
    result=Client.metrics(SimpleNamespace(pid=pid))
    result['within_memory_cap']=result['memory_peak_bytes']<=128*1024*1024
    return result

def execute(client,sql):
    actual=client.execute(sql)
    if actual.get('error'):raise AssertionError((sql[:200],actual))
    return actual

def load(client,batches):
    execute(client,'CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(128))')
    started=time.perf_counter()
    for batch in batches:execute(client,batch)
    return time.perf_counter()-started

def prepare_shared(seed):
    if args.rows<100000:return prepare(seed,args.rows,128,args.queries,args.distribution)
    rng=np.random.default_rng(seed)
    sample=rng.normal if args.distribution=='normal' else lambda size:rng.uniform(-1,1,size=size)
    vectors=sample(size=(args.rows,128)).astype(np.float32);probes=sample(size=(args.queries,128)).astype(np.float32)
    double=vectors.astype(np.float64);ids=np.arange(1,args.rows+1);tags=ids%10
    norms=np.einsum('ij,ij->i',double,double);requests=[];truth=[]
    for start in range(0,args.queries,32):
        block=probes[start:start+32].astype(np.float64);qn=np.einsum('ij,ij->i',block,block)
        approximate=norms[:,None]+qn[None,:]-2*(double@block.T)
        for j,query in enumerate(block):
            index=start+j;candidates=np.flatnonzero(tags==index%10) if index%2 else np.arange(args.rows)
            scores=approximate[candidates,j];k=min(10,len(candidates))
            boundary=np.partition(scores,k-1)[k-1]
            # Norm/dot evaluation error is bounded by gamma_d times norm sums.
            # Keep a wide outward interval, then compute direct Euclidean truth.
            slack=64*np.finfo(np.float64).eps*128*(float(norms.max())+float(qn[j])+1)
            eligible=candidates[scores<=boundary+slack]
            distances=np.linalg.norm(double[eligible]-query,axis=1)
            order=np.lexsort((ids[eligible],distances))[:k]
            truth.append([(int(ids[eligible[i]]),float(distances[i])) for i in order])
            where=f' WHERE tag={index%10}' if index%2 else ''
            requests.append(f'SELECT id,L2_DISTANCE(v,{vector_sql(probes[index])}) AS d FROM t{where} ORDER BY d LIMIT 10')
    batches=[]
    for start in range(0,args.rows,100):
        values=','.join(f'({i+1},{int(tags[i])},{vector_sql(vectors[i])})' for i in range(start,min(start+100,args.rows)))
        batches.append('INSERT INTO t VALUES '+values)
    return vectors,batches,requests,truth

try:
    print('preparing shared dataset and independent truth',flush=True)
    fixed=prepare_shared(args.seed) if args.seed is not None else None
    for number in range(1,args.rounds+1):
        seed=args.seed if args.seed is not None else 719+number
        vectors,batches,requests,truth=fixed if fixed is not None else prepare_shared(seed)
        digest=hashlib.sha256(memoryview(vectors))
        for request in requests:digest.update(request.encode())
        old_digest=report.setdefault('datasets',{}).get(str(seed))
        if old_digest and old_digest!=digest.hexdigest():raise ValueError('resume dataset mismatch')
        report['datasets'][str(seed)]=digest.hexdigest();save()
        common=[]
        for sql in requests:
            match=re.fullmatch(r'SELECT id,(L2_DISTANCE\(.*\)) AS d FROM t(.*?) ORDER BY d LIMIT 10',sql)
            common.append('SELECT id FROM t'+match[2]+' ORDER BY '+match[1]+' LIMIT 10')
        order=('ours','teammate') if number%2 else ('teammate','ours')
        for version in order:
            print(f'round {number}: {version}',flush=True)
            item=next((r for r in report['rounds'] if r['round']==number and r['version']==version),None)
            if item is None:item={'round':number,'seed':seed,'version':version};report['rounds'].append(item)
            if 'query' in item:continue
            if 'write' not in item:
                with session(version,number,'write') as (client,name):
                    item['write']={'seconds':load(client,batches),'resources':metrics(name)}
                    print(version,'write',item['write']['seconds'],flush=True)
                    save()
            # Verify confirmed writes after a process/container restart, outside write timing.
            old=args.output/version/str(number)/'write'/'data'
            with session(version,number,'write-verify',old) as (client,name):
                actual=execute(client,'SELECT id,tag,v FROM t')
                if actual.get('text_rows'):decode_text(actual,{'rows':[[1,1,[0.]*128]]})
                actual['rows'].sort(key=lambda row:row[0])
                assert len(actual['rows'])==args.rows
                for index,(key,tag,vector) in enumerate(actual['rows']):
                    assert key==index+1 and tag==key%10 and np.array_equal(np.asarray(vector,dtype=np.float32),vectors[index])
                item['write']['restart_content_correct']=True
                del actual
            if args.reuse_write_data:
                for path in old.rglob('*'):
                    if path.is_file():
                        with path.open('rb') as source:os.posix_fadvise(source.fileno(),0,0,os.POSIX_FADV_DONTNEED)
            with session(version,number,'query',old if args.reuse_write_data else None) as (client,name):
                if not args.reuse_write_data:load(client,batches)
                execute(client,'CREATE VECTOR INDEX ix ON t(v)')
                for sql in common[:10]:execute(client,sql)
                started=time.perf_counter();answers=[execute(client,sql) for sql in common]
                elapsed=time.perf_counter()-started;resources=metrics(name)
                print(version,'query',elapsed,flush=True)
            exact=0;recalls={False:[],True:[]}
            for index,(actual,wanted) in enumerate(zip(answers,truth)):
                if actual.get('text_rows'):decode_text(actual,{'rows':[[1]]})
                keys=[row[0] for row in actual['rows']];expected=[key for key,distance in wanted]
                assert len(keys)==len(expected) and len(set(keys))==len(keys)
                assert all(type(key) is int and 1<=key<=args.rows and (index%2==0 or key%10==index%10) for key in keys)
                exact+=keys==expected;recalls[bool(index%2)].append(len(set(keys)&set(expected))/len(expected))
            item['query']={'seconds':elapsed,'resources':resources,'exact_order_queries':exact,'total_queries':args.queries,
                           'recall':{str(filtered):statistics.mean(values) for filtered,values in recalls.items()}}
            item['status']='PASS' if exact==args.queries else 'EXACT_MISMATCH'
            if not all(item[track]['resources']['within_memory_cap'] for track in ('write','query')):item['status']='RESOURCE_LIMIT'
            save()
    report['summary']={}
    for version in ('ours','teammate'):
        runs=[r for r in report['rounds'] if r['version']==version]
        report['summary'][version]={'write_seconds_median':statistics.median(r['write']['seconds'] for r in runs),
            'query_seconds_median':statistics.median(r['query']['seconds'] for r in runs),
            'query_peak_bytes_median':statistics.median(r['query']['resources']['memory_peak_bytes'] for r in runs),
            'write_io_bytes_median':statistics.median(r['write']['resources']['io_bytes'] for r in runs),
            'query_io_bytes_median':statistics.median(r['query']['resources']['io_bytes'] for r in runs)}
    report['status']='PASS' if all(r['status']=='PASS' for r in report['rounds']) else 'EXACT_MISMATCH'
except BaseException as error:
    report['status']='FAIL';report['error']=repr(error);raise
finally:save()
print(json.dumps(report['summary'],indent=2))
raise SystemExit(0 if report['status']=='PASS' else 1)
