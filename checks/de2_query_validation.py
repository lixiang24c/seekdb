#!/usr/bin/env python3
"""Independent truth gates for the de2 query patch; no performance measurements."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import struct
import sys
import traceback
import zlib

from queryable_regression import Fixture, q
from native_validation import f32, identical


class EvidenceFixture(Fixture):
    def __init__(self,args,label):
        super().__init__(args,label)
        self.label=label
        self.transcript=args.output/(label+'.requests.jsonl')
    def request(self,sql=None,*a,**kw):
        with self.transcript.open('a') as f:f.write(json.dumps({'sql':sql,'kwargs':kw},ensure_ascii=False)+'\n')
        return super().request(sql,*a,**kw)
    def close(self):
        if sys.exc_info()[0] is not None:
            self.stop()
            shutil.copytree(self.root,self.args.output/(self.label+'-failed-data'),dirs_exist_ok=True)
        super().close()


def diagnostics(s):
    found={}
    for line in s.error_path.read_text().splitlines():
        if '_PATH ' in line:
            name,body=line.split(' ',1)
            found[name]=json.loads(body)
    return found


def verify(s,state,vector,group=None,k=10,table='t',projection='distance',op='=',bound=None):
    def accepts(i,row):
        if group is not None and row[0]!=group:return False
        if bound is not None and not {'=':i==bound,'>':i>bound,'<':i<bound,'>=':i>=bound,'<=':i<=bound}[op]:return False
        return True
    # State was populated by the generator and transactions, never by SELECT.
    ranked=sorted((math.fsum((x-y)*(x-y) for x,y in zip(row[1],vector)),i) for i,row in state.items() if accepts(i,row))[:k]
    expr='L2_DISTANCE(v,'+json.dumps(vector)+')'
    cols={'id':'id','constant_plain':'id,g','scalar_plain':'body,g,id','distance':f'id,{expr} AS d','constant':f'id,g,{expr} AS d','scalar':f'body,g,id,{expr} AS d','other':f'id,L2_DISTANCE(v,{json.dumps([0.]*len(vector))}) AS d'}[projection]
    where=[]
    if group is not None:where.append('g='+str(group))
    if bound is not None:where.append('id'+op+str(bound))
    order='d' if projection in ('distance','constant','scalar') else expr
    sql=f'SELECT {cols} FROM {table} '+('WHERE '+' AND '.join(where)+' ' if where else '')+f'ORDER BY {order} LIMIT {k}'
    expected=[]
    for score,i in ranked:
        row=state[i];dist=math.sqrt(score)
        expected.append([i] if projection=='id' else [i,dist] if projection=='distance' else [i,row[0],dist] if projection=='constant' else [row[2],row[0],i,dist] if projection=='scalar' else [i,row[0]] if projection=='constant_plain' else [row[2],row[0],i] if projection=='scalar_plain' else [i,math.sqrt(math.fsum(x*x for x in row[1]))])
    actual=q(s,sql)['rows']
    try:identical(actual,expected)
    except BaseException:
        (s.args.output/'mismatch.json').write_text(json.dumps({'sql':sql,'expected':expected,'actual':actual,'state':state},ensure_ascii=False))
        raise


def load(s,state,table='t',dim=128,create=True):
    if create:q(s,f'CREATE TABLE {table}(id BIGINT PRIMARY KEY,g BIGINT,body VARCHAR(8192),v VECTOR({dim}))')
    keys=list(state)
    for start in range(0,len(keys),64):
        q(s,f'INSERT INTO {table} VALUES '+','.join(f"({i},{state[i][0]},'"+state[i][2].replace("'","''")+"',"+json.dumps(state[i][1])+')' for i in keys[start:start+64]))
    if create:q(s,f'CREATE VECTOR INDEX ix ON {table}(v)')


def numeric(args):
    rng=random.Random(43);count=0;records={}
    shapes=('gaussian','near','ties','subnormal','maximum','heterogeneous','scaled_small','normalized','sparse','uniform')
    for shape in shapes:
        s=EvidenceFixture(args,'truth-'+shape);n=1153;dim=128
        def vector(i):
            if shape=='near':return [f32(1+rng.randrange(-2,3)*2**-23) for _ in range(dim)]
            if shape=='ties':return [(-1. if i%2 else 1.)]+[0.]*(dim-1)
            if shape=='uniform':return [f32(.3)]*dim
            if shape=='subnormal':return [rng.choice([-2**-149,0.,2**-149,2**-126]) for _ in range(dim)]
            if shape=='maximum':return [rng.choice([-1.,0.,1.])*f32(3.4028234663852886e38) for _ in range(dim)]
            scale=2.**-80 if shape=='scaled_small' else 1e30 if shape=='heterogeneous' and i%131==0 else 1.
            v=[f32(rng.gauss(0,1)*scale) for _ in range(dim)]
            if shape=='normalized':
                norm=math.sqrt(math.fsum(x*x for x in v));v=[f32(x/norm) for x in v]
            return v
        state={i-576:[i%37,vector(i),'中'+str(i)] for i in range(n)}
        try:
            s.start();load(s,state)
            for turn in range(12):
                v=vector(-1) if turn%3 else [0.]*dim
                if shape=='sparse':v=[x if j<2*(turn%17) else 0. for j,x in enumerate(v)]
                group=turn%7 if turn%2 else None;k=(1,10,32,127,1024,10)[turn%6]
                for projection in ('id','distance'):
                    verify(s,state,v,group,k,projection=projection);count+=1
            s.shutdown();records[shape]=diagnostics(s)
        finally:s.close()
    env=dict(x.split('=',1) for x in args.env)
    d=records['gaussian']['MINIDB_PATH'];block=records['gaussian']['BLOCK_PATH']
    assert d['posting_scans']==0
    if env.get('MINIDB_INDEX_MMAP')=='0':assert d['mapped_opens']==block['scans']==0 and d['buffered_scans']>0
    if env.get('MINIDB_SIMD')=='0':assert d['simd']==block['scans']==0
    if args.require_fast:
        assert block['scans']>0 and block['certified']>0,records['gaussian']
        assert records['heterogeneous']['TILE_BOUND_PATH']['scans']>0
        assert records['uniform']['UNIFORM_PATH']['scans']>0
        assert records['sparse']['SPARSE_QUERY_PATH']['scans']>0
        assert records['gaussian']['BOUND_PRECISION_PATH']['fp64']>0
        assert records['gaussian']['REFINE_FOUR_PATH']['calls']>0
    return {'queries':count,'shapes':list(shapes),'oracle':'independent float32 data, binary64 rounded products + math.fsum; IDs and binary64 distance bytes','diagnostics':records}


def plans(args):
    s=EvidenceFixture(args,'plans');rng=random.Random(44);count=0
    state={i-576:[i%13,[f32(rng.gauss(0,1)) for _ in range(128)],'宽行😀'+str(i)+'x'*4096] for i in range(1153)}
    try:
        s.start();load(s,state)
        def cycle(full=False):
            nonlocal count
            # Six variants promote a parameterized plan. New vectors and values
            # must bind every time, including predicate-derived output constants.
            for projection in ('id','distance','constant','scalar','constant_plain','scalar_plain'):
                for group in range(13 if full else 7):
                    v=[f32(rng.gauss(0,1)) for _ in range(128)]
                    verify(s,state,v,group,10,projection=projection);count+=1
            for group in (-2**63,2**63-1,-1,99):
                verify(s,state,[0.]*128,group,10,projection='constant');count+=1
            for k in (1,64,1024):
                verify(s,state,[0.]*128,None,k,projection='distance');count+=1
            verify(s,state,[1.]*128,0,10,projection='other');count+=1
        cycle(True)
        s.shutdown();parameter_probe=diagnostics(s);s.start()
        env=dict(x.split('=',1) for x in args.env)
        if env.get('MINIDB_QUERY_PLANS')!='0' and env.get('MINIDB_PARAMETER_PLANS')!='0':
            assert parameter_probe['MINIDB_PATH']['plan_hits']>=21,parameter_probe
        for ending in ('ROLLBACK','COMMIT'):
            saved=copy.deepcopy(state);q(s,'BEGIN')
            q(s,'UPDATE t SET g=2,v='+json.dumps([0.]*128)+' WHERE id=-576');state[-576][0:2]=[2,[0.]*128]
            q(s,'DELETE FROM t WHERE id=0');state.pop(0)
            q(s,"INSERT INTO t VALUES (0,3,'reinsert',"+json.dumps([1.]*128)+')');state[0]=[3,[1.]*128,'reinsert']
            assert not s.request("INSERT INTO t VALUES (9999,0,'partial',"+json.dumps([0.]*128)+"),(0,0,'duplicate',"+json.dumps([0.]*128)+')')['ok']
            assert q(s,'SELECT id FROM t WHERE id=9999')['rows']==[]
            cycle();q(s,ending)
            if ending=='ROLLBACK':state=saved
            cycle()
        # Full deletion/repopulation also exercises checkpoint invalidation.
        q(s,'DELETE FROM t');state={i:[i%13,[f32(i/100)]*128,'fresh'] for i in range(1153)};load(s,state,create=False);cycle()
        s.shutdown();before=diagnostics(s)
        s.start();cycle();s.shutdown();after=diagnostics(s)
        env=dict(x.split('=',1) for x in args.env)
        if env.get('MINIDB_QUERY_PLANS')!='0':assert before['MINIDB_PATH']['plan_hits']>0 and after['MINIDB_PATH']['plan_hits']>0
        else:assert before['MINIDB_PATH']['plan_hits']==0
        return {'queries':count,'parameter_values':13,'parameter_probe':parameter_probe,'failed_statement_atomic':True,'endings':['ROLLBACK','COMMIT','restart'],'diagnostics_before_restart':before,'diagnostics':after}
    finally:s.close()


def crc(args):
    results=[]
    for point in ('write-3','write-4','write-5','write-6','sync-before','sync-after'):
        s=EvidenceFixture(args,'crc-'+point)
        try:
            s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,body VARCHAR(8192),v VECTOR(128))')
            sql='INSERT INTO t VALUES '+','.join(f"({i},1,'"+'x'*1024+"',"+json.dumps([float(i)]*128)+')' for i in range(100))
            s.arm(point);s.request(sql,wait=False);s.kill_at_marker(point)
            s.start();rows=q(s,'SELECT id FROM t ORDER BY id')['rows'];expected=[[i] for i in range(100)]
            assert rows in ([expected] if point=='sync-after' else [[],expected])
            if rows:
                identical(q(s,'SELECT id,g,v FROM t ORDER BY id')['rows'],[[i,1,[float(i)]*128] for i in range(100)])
            s.shutdown();raw=(s.root/'queryable.log').read_bytes();at=0;frames=0;big=0
            while at<len(raw):
                size,meta=struct.unpack_from('<QI',raw,at);end=at+12+size
                assert meta<=size and end+12<=len(raw)
                assert struct.unpack_from('<I',raw,end)[0]==zlib.crc32(raw[at:end])
                assert raw[end+4:end+12]==b'MVCMT001'
                big+=size-meta>65536;frames+=1;at=end+12
            assert at==len(raw)
            if point=='sync-after':assert big>0
            s.start();assert q(s,'SELECT id FROM t ORDER BY id')['rows']==rows;s.shutdown()
            results.append({'point':point,'rows':len(rows),'frames':frames,'multi_buffer_frames':big,'crc_sha256':hashlib.sha256(raw).hexdigest(),'status':'PASS'})
        finally:s.close()
    return results


def large(args):
    s=EvidenceFixture(args,'large-map');rng=random.Random(42);count=0
    state={i:[i%13,[f32(rng.gauss(0,1)) for _ in range(128)],'x'*8192] for i in range(8192)}
    try:
        s.start();load(s,state)
        size=(s.root/'queryable.log').stat().st_size;assert size>64<<20
        def check():
            nonlocal count
            for group in (None,2):
                for projection in ('id','distance'):
                    verify(s,state,[0.]*128,group,10,projection=projection);count+=1
        check();saved=copy.deepcopy(state)
        for ending in ('ROLLBACK','COMMIT','KILL'):
            q(s,'BEGIN');q(s,"UPDATE t SET g=2,body='changed',v="+json.dumps([0.]*128)+' WHERE id=0');state[0]=[2,[0.]*128,'changed'];check()
            if ending=='KILL':s.crash();s.start();state=copy.deepcopy(saved)
            else:
                q(s,ending)
                if ending=='ROLLBACK':state=copy.deepcopy(saved)
                else:saved=copy.deepcopy(state)
            check()
        s.shutdown();d=diagnostics(s)
        env=dict(x.split('=',1) for x in args.env)
        if env.get('MINIDB_INDEX_MMAP')=='0':assert d['MINIDB_PATH']['mapped_opens']==0
        else:assert d['MINIDB_PATH']['mapped_opens']>0
        return {'queries':count,'log_bytes':size,'rows':len(state),'diagnostics':d}
    finally:s.close()


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--binary');g.add_argument('--image')
    p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cpus',default='1');p.add_argument('--memory',default='2g');p.add_argument('--env',action='append',default=[]);p.add_argument('--cgroupns',default=None)
    p.add_argument('--require-fast',action='store_true');p.add_argument('--section',choices=['numeric','plans','crc','large','all'],default='all');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','host_only':bool(args.binary),'checks':{},'args':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}}
    try:
        for name,fn in [('numeric',numeric),('plans',plans),('crc',crc),('large',large)]:
            if args.section==name or (args.section=='all' and name!='large'):report['checks'][name]=fn(args);print(name,'PASS',flush=True)
        report['status']='PASS'
    except BaseException as e:report.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
    finally:(args.output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
if __name__=='__main__':main()
