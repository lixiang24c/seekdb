#!/usr/bin/env python3
"""Focused 2f9 migration truth. Diagnostics are never benchmark results."""
import argparse
import copy
import json
import math
from pathlib import Path
import random
import struct
import traceback
from de2_query_validation import EvidenceFixture, diagnostics, load
from queryable_regression import q
from native_validation import f32, identical


def check(s,state,query,project=None,conditions=(),k=10,text=False):
    project=query if project is None else project
    def accept(row):
        for op,value in conditions:
            actual=row[0]
            if not {'=':actual==value,'<':actual<value,'<=':actual<=value,'>':actual>value,'>=':actual>=value}[op]:return False
        return True
    rank=sorted((math.fsum((x-y)*(x-y) for x,y in zip(row[1],query)),i) for i,row in state.items() if accept(row))[:k]
    def literal(v):
        raw=json.dumps(v,separators=(',',':'));return "'"+raw+"'" if text else raw
    sql=f'SELECT id,L2_DISTANCE(v,{literal(project)}) AS d FROM t '+('WHERE '+' AND '.join('g'+op+str(v) for op,v in conditions)+' ' if conditions else '')+f'ORDER BY L2_DISTANCE(v,{literal(query)}) LIMIT {k}'
    expected=[[i,math.sqrt(math.fsum((x-y)*(x-y) for x,y in zip(state[i][1],project)))] for _,i in rank]
    actual=q(s,sql)['rows']
    try:identical(actual,expected)
    except BaseException:
        (s.args.output/'mismatch.json').write_text(json.dumps({'sql':sql,'expected':expected,'actual':actual,'state':state}));raise
    return 1


def normal(rng,dim):return [f32(rng.gauss(0,1)) for _ in range(dim)]

def numeric(args):
    rng=random.Random(424344);results={};total=0
    cases=[(d,'random') for d in (64,256,384,512)]+[(512,s) for s in ('near','subnormal','maximum','heterogeneous','zero','dot-limit','scaled-small')]
    for dim,shape in cases:
        s=EvidenceFixture(args,f'{dim}-{shape}')
        def vector(i):
            if shape=='near':return [f32(1+rng.randrange(-2,3)*2**-23) for _ in range(dim)]
            if shape=='subnormal':return [rng.choice([-2**-149,0.,2**-149,2**-126]) for _ in range(dim)]
            if shape=='maximum':return [rng.choice([-1.,0.,1.])*f32(3.4028234663852886e38) for _ in range(dim)]
            if shape=='zero':return [-0. if i%2 else 0.]*dim
            if shape=='dot-limit':return [(-1. if i%2 else 1.)]*dim
            scale=2**-80 if shape=='scaled-small' else 1e30 if shape=='heterogeneous' and i%131==0 else 1.
            return [f32(x*scale) for x in normal(rng,dim)]
        state={i-576:[i%10,vector(i),'中😀'+str(i)] for i in range(1153)}
        try:
            s.start();load(s,state,dim=dim)
            for turn in range(12):
                v=vector(-1) if turn%3 else [0.]*dim
                total+=check(s,state,v,conditions=[('=',turn%10)] if turn%2 else [],k=(1,10,64,1024)[turn%4])
            s.shutdown();d=diagnostics(s);results[f'{dim}-{shape}']=d
            if args.require_fast and shape=='random':assert d['DYNAMIC_BLOCK_PATH']['scans']>0,d
            flags=dict(x.split('=',1) for x in args.env)
            if any(flags.get(k)=='0' for k in ('MINIDB_DYNAMIC_BLOCKS','MINIDB_SIMD','MINIDB_INDEX_MMAP')):assert d['DYNAMIC_BLOCK_PATH']['scans']==0,d
            if flags.get('MINIDB_INDEX_MMAP')=='0':assert d['MINIDB_PATH']['mapped_opens']==0,d
            if flags.get('MINIDB_SIMD')=='0':assert d['MINIDB_PATH']['simd']==0,d
        finally:s.close()
    return {'queries':total,'diagnostics':results,'oracle':'independent float32 state; math.fsum of binary64 products; binary64 distance bytes'}


def repeated(args):
    rng=random.Random(43);state={i-576:[i%13,normal(rng,128),'same'] for i in range(1153)};s=EvidenceFixture(args,'repeated');total=0
    def cycle():
        nonlocal total
        for turn in range(16):
            v=normal(rng,128);w=normal(rng,128)
            total+=check(s,state,v,conditions=[('=',turn%13)] if turn%2 else [])
            total+=check(s,state,v,project=w,conditions=[('=',turn%13)] if turn%2 else [])
    try:
        s.start();load(s,state);cycle();s.shutdown();initial=diagnostics(s)
        if args.require_fast:assert initial['MINIDB_PATH']['plan_hits']>0,initial
        s.start();cycle()
        for ending in ('ROLLBACK','COMMIT'):
            saved=copy.deepcopy(state);q(s,'BEGIN');q(s,'UPDATE t SET g=2,v='+json.dumps([0.]*128)+' WHERE id=-576');state[-576][:2]=[2,[0.]*128]
            q(s,'DELETE FROM t WHERE id=0');state.pop(0)
            q(s,"INSERT INTO t VALUES (0,2,'replacement',"+json.dumps([1.]*128)+')');state[0]=[2,[1.]*128,'replacement']
            assert not s.request("INSERT INTO t VALUES (9999,0,'partial',"+json.dumps([0.]*128)+"),(0,0,'dup',"+json.dumps([0.]*128)+')')['ok']
            assert q(s,'SELECT id FROM t WHERE id=9999')['rows']==[]
            cycle();q(s,ending)
            if ending=='ROLLBACK':state=saved
            cycle()
        s.shutdown();s.start();cycle();s.shutdown();final=diagnostics(s)
        # A fresh process with only string literals must not create text plans.
        s.start()
        for _ in range(8):total+=check(s,state,normal(rng,128),text=True)
        s.shutdown();text=diagnostics(s)
        if args.allow_text_plans:
            if dict(x.split('=',1) for x in args.env).get('MINIDB_QUERY_PLANS')!='0':assert text['MINIDB_PATH']['plan_hits']>0,text
        else:assert text['MINIDB_PATH']['plan_hits']==0 and text['MINIDB_PATH']['plan_builds']==0,text
        return {'queries':total,'initial':initial,'final':final,'text_plan_probe':text,'text_plans_allowed':args.allow_text_plans,'different_vectors_and_transaction_rebind':True}
    finally:s.close()


def secondary(args):
    rng=random.Random(44);n=4096;dim=128;state={i:[i%1000,normal(rng,dim),'前置😀'+str(i)] for i in range(n)}
    state[0][0]=-2**63;state[1][0]=2**63-1;s=EvidenceFixture(args,'secondary');total=0
    def cycle():
        nonlocal total
        filters=[[('=',7)],[('=',999)],[('=',-1)],[('=',-2**63)],[('=',2**63-1)],[('>=',7),('<=',8)],[('>=',0)],[('=',7),('>',8)],[('=',8),('=',7)],[('<',15)],[('<=',16)]]
        for c in filters:total+=check(s,state,normal(rng,128),conditions=c)
    try:
        s.start()
        # Text precedes the vector; indexed BIGINT follows it.
        q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,body VARCHAR(8192),v VECTOR(128),g BIGINT)')
        for start in range(0,n,100):q(s,'INSERT INTO t VALUES '+','.join(f"({i},'{state[i][2]}',"+json.dumps(state[i][1])+f',{state[i][0]})' for i in range(start,min(n,start+100))))
        q(s,'CREATE VECTOR INDEX ix ON t(v)');cycle();cycle();s.shutdown();initial=diagnostics(s)
        env=dict(x.split('=',1) for x in args.env)
        if args.require_fast:assert initial['SECONDARY_PATH']['builds']>0 and initial['SECONDARY_PATH']['scans']>0,initial
        if env.get('MINIDB_SECONDARY_FILTER')=='0' or env.get('MINIDB_INDEX_MMAP')=='0':assert initial['SECONDARY_PATH']['scans']==0
        s.start();cycle()
        for ending in ('ROLLBACK','COMMIT'):
            saved=copy.deepcopy(state);q(s,'BEGIN');q(s,'UPDATE t SET g=7,v='+json.dumps([0.]*128)+' WHERE id=1');state[1][:2]=[7,[0.]*128];cycle()
            q(s,'DELETE FROM t WHERE id=7');state.pop(7)
            q(s,"INSERT INTO t VALUES (7,'reinsert',"+json.dumps([0.]*128)+',7)');state[7]=[7,[0.]*128,'reinsert'];cycle()
            assert not s.request("INSERT INTO t VALUES (9999,'partial',"+json.dumps([0.]*128)+",7),(7,'dup',"+json.dumps([0.]*128)+',7)')['ok'];assert q(s,'SELECT id FROM t WHERE id=9999')['rows']==[]
            cycle();q(s,ending)
            if ending=='ROLLBACK':state=saved
            cycle()
        s.shutdown();s.start();cycle();s.shutdown();final=diagnostics(s)
        return {'queries':total,'initial':initial,'final':final,'wide_range_and_empty_and_bigint_bounds':True,'update_delete_reinsert_rollback_commit_restart':True}
    finally:s.close()


def low(args):
    results={};rng=random.Random(42)
    for dim in (64,512):
        s=EvidenceFixture(args,'low-'+str(dim));state={i:[i%10,normal(rng,dim),''] for i in range(8192)}
        try:
            s.start();load(s,state,dim=dim)
            for turn in range(8):check(s,state,normal(rng,dim),conditions=[('=',turn%10)] if turn%2 else [])
            s.shutdown();d=diagnostics(s);results[str(dim)]=d
            if args.image and args.memory=='16m':
                assert d['MINIDB_PATH']['budget_bytes']==(16<<20)*7//8,d
                if dim==512:assert d['DYNAMIC_BLOCK_PATH']['scans']==0 and d['BLOCK_PATH']['builds']==0,d
                elif args.require_fast:assert d['DYNAMIC_BLOCK_PATH']['scans']>0,d
        finally:s.close()
    return {'queries':16,'rows':8192,'diagnostics':results}


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--binary');g.add_argument('--image')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--memory',default='384m');p.add_argument('--cpus',default='1');p.add_argument('--cgroupns',default=None);p.add_argument('--env',action='append',default=[]);p.add_argument('--require-fast',action='store_true');p.add_argument('--allow-text-plans',action='store_true');p.add_argument('--section',choices=['numeric','repeated','secondary','low','all'],default='all');a=p.parse_args();a.env+=['MINIDB_POSTINGS=0']
    a.output.mkdir(parents=True,exist_ok=False);r={'status':'RUNNING','host_only':bool(a.binary),'checks':{},'args':{k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()}}
    try:
        for name in ('numeric','repeated','secondary','low'):
            if a.section==name or a.section=='all':r['checks'][name]=globals()[name](a);print(name,'PASS',flush=True)
        r['status']='PASS'
    except BaseException as e:r.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
    finally:(a.output/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
