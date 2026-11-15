#!/usr/bin/env python3
"""Second migration: independently modeled text plans and partial-extent TopK."""
import argparse
import copy
import json
import math
from pathlib import Path
import random
import struct
import sys
import traceback
from de2_query_validation import EvidenceFixture,diagnostics
from native_validation import f32,identical
from queryable_regression import q
from minidb.sql import vector as parse_vector,DBError


def literal(s):return "'"+s.replace("'","''")+"'"
def raw(v):return json.dumps(v,separators=(',',':'))
def normal(rng,dim):return [f32(rng.gauss(0,1)) for _ in range(dim)]

class Truth:
    def __init__(self,state):
        self.state=state;self.ids=sorted(state);self.np=None
        try:
            import numpy as np
            self.np=np;self.matrix=np.array([state[i][1] for i in self.ids],dtype=np.float64)
        except ImportError:pass
    def rows(self,v,conditions=(),k=10,project=None):
        def accepts(row):
            for col,op,b in conditions:
                a=row[0] if col=='g' else row[3]
                if not {'=':a==b,'>':a>b,'<':a<b,'>=':a>=b,'<=':a<=b}[op]:return False
            return True
        ix=[j for j,i in enumerate(self.ids) if accepts(self.state[i])];candidates=ix
        if self.np is not None and len(ix)>k and k>0:
            np=self.np;sums=np.empty(len(ix));query=np.array(v,dtype=np.float64)
            for start in range(0,len(ix),2048):
                delta=self.matrix[ix[start:start+2048]]-query;sums[start:start+2048]=np.sum(delta*delta,axis=1)
            eps=8*len(v)*2**-52;lower=np.nextafter(sums/(1+eps),-np.inf);upper=np.nextafter(sums/(1-eps),np.inf)
            cutoff=float(np.partition(upper,k-1)[k-1]);candidates=[ix[j] for j in np.flatnonzero(lower<=cutoff)]
        rank=sorted((math.fsum((x-y)*(x-y) for x,y in zip(self.state[self.ids[j]][1],v)),self.ids[j]) for j in candidates)[:k]
        if self.np is not None and len(ix)>k and k>0:
            omitted=lower[lower>cutoff];assert not len(omitted) or float(omitted.min())>rank[-1][0]
        return [[i] if project is None else [i,math.sqrt(math.fsum((x-y)*(x-y) for x,y in zip(self.state[i][1],project)))] for score,i in rank]


def check(s,truth,v,conditions=(),k=10,project=None,order_literal=None,project_literal=None):
    expr='L2_DISTANCE(v,'+(order_literal if order_literal is not None else raw(v))+')'
    cols='id' if project is None else 'id,L2_DISTANCE(v,'+(project_literal if project_literal is not None else raw(project))+') AS d'
    sql='SELECT '+cols+' FROM t '+('WHERE '+' AND '.join(col+op+str(b) for col,op,b in conditions)+' ' if conditions else '')+f'ORDER BY {expr} LIMIT {k}'
    want=truth.rows(v,conditions,k,project);got=q(s,sql)['rows']
    try:identical(got,want)
    except BaseException:
        (s.args.output/'mismatch.json').write_text(json.dumps({'sql':sql,'actual':got,'expected':want,'state':truth.state}));raise
    return 1


def load(s,state,dim):
    q(s,f'CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,body VARCHAR(256),v VECTOR({dim}),h BIGINT)');ids=list(state)
    for start in range(0,len(ids),100):q(s,'INSERT INTO t VALUES '+','.join(f'({i},{state[i][0]},'+literal(state[i][2])+','+raw(state[i][1])+f',{state[i][3]})' for i in ids[start:start+100]))
    q(s,'CREATE VECTOR INDEX ix ON t(v)')


def strings(args):
    rng=random.Random(43);state={i:[i%16,normal(rng,128),"a'b中😀",i%7] for i in range(1153)};s=EvidenceFixture(args,'strings');count=0;records={}
    try:
        s.start();load(s,state,128);truth=Truth(state)
        # Rebinding integer filters must promote a parameterized string plan.
        for turn in range(160):
            v=normal(rng,128);count+=check(s,truth,v,[('g','=',turn%16)],order_literal=literal(raw(v)))
        s.shutdown();records['parameterized']=diagnostics(s)
        enabled=dict(x.split('=',1) for x in args.env).get('MINIDB_QUERY_PLANS')!='0'
        if args.require_fast and enabled:assert records['parameterized']['MINIDB_PATH']['plan_hits']>=140,records
        s.start()
        def cycle():
            nonlocal count
            for turn in range(16):
                v=normal(rng,128);w=normal(rng,128);c=[('g','=',turn)] if turn%2 else []
                count+=check(s,truth,v,c,project=v,order_literal=literal(raw(v)),project_literal=literal(raw(v)))
                count+=check(s,truth,v,c,project=w,order_literal=literal(raw(v)),project_literal=literal(raw(w)))
        cycle()
        for ending in ('ROLLBACK','COMMIT'):
            saved=copy.deepcopy(state);q(s,'BEGIN');q(s,'UPDATE t SET v='+raw([0.]*128)+',g=7 WHERE id=0');state[0][:2]=[7,[0.]*128]
            q(s,'DELETE FROM t WHERE id=7');state.pop(7);q(s,"INSERT INTO t VALUES (7,7,'reinsert',"+raw([0.]*128)+',0)');state[7]=[7,[0.]*128,'reinsert',0]
            assert not s.request("INSERT INTO t VALUES (9999,7,'partial',"+raw([0.]*128)+",0),(7,7,'duplicate',"+raw([0.]*128)+',0)')['ok'];assert q(s,'SELECT id FROM t WHERE id=9999')['rows']==[]
            truth=Truth(state);cycle();q(s,ending)
            if ending=='ROLLBACK':state=saved
            truth=Truth(state);cycle()
        s.shutdown();s.start();cycle();s.shutdown();records['mixed_and_transactions']=diagnostics(s)
        # Fresh process: whitespace *outside* brackets cannot use the template.
        s.start()
        for pad in (' ','\t\n','\u2003'):
            for _ in range(3):
                v=normal(rng,128);count+=check(s,truth,v,order_literal=literal(pad+raw(v)+pad))
        s.shutdown();records['outer_whitespace']=diagnostics(s);assert records['outer_whitespace']['MINIDB_PATH']['plan_hits']==0
        s.start()
        for sp in (' ','\t','\n','\u00a0','\u2003'):
            text='['+(','+sp).join(['1e-3','-0','+2.5']+['0']*125)+']';v=parse_vector(text,128)
            for _ in range(2):count+=check(s,truth,v,[('g','=',7)],project=v,order_literal=literal(text),project_literal=literal(text))
        # Invalid SQL-string contents after populated templates; SQL doubled
        # quotes must decode before vector validation, not become numeric data.
        bad=['[1,\'x\']','[1,\\n2]','["1",2]','[true,0]','[1e309]','[1e39]','[1,,2]','[1,]','[]','[0]','[0] trailing',"[0]'; SELECT id FROM t --"]
        for text in bad:
            try:parse_vector(text,128)
            except DBError as e:code=e.code
            else:raise AssertionError(('expected invalid vector',text))
            sql='SELECT id FROM t ORDER BY L2_DISTANCE(v,'+literal(text)+') LIMIT 10';response=s.request(sql)
            assert not response['ok'] and response['error']['code']==code,(text,code,response)
            count+=1
        # Same prepared shape alternates valid, invalid, and different dimension.
        for _ in range(3):
            v=normal(rng,128);count+=check(s,truth,v,order_literal=literal(raw(v)))
            for text in ('[0]','['+','.join(['1e309']+['0']*127)+']'):
                r=s.request('SELECT id FROM t ORDER BY L2_DISTANCE(v,'+literal(text)+') LIMIT 10');assert not r['ok'];count+=1
        s.shutdown();records['whitespace_and_invalid']=diagnostics(s)
        return {'queries':count,'diagnostics':records,'oracle':'independent state + math.fsum products, binary64 distance bytes; Python vector parser for invalid text codes'}
    finally:s.close()


def partial(args):
    results={};total=0;rng=random.Random(42);env=dict(x.split('=',1) for x in args.env)
    # Host needs >8MiB compressed data to reach the cap; container tests use the
    # documented low-memory sizes. Host numbers are never 32MiB evidence.
    shapes=[(128,30000),(512,20000 if args.binary else 10000)]
    for dim,n in shapes:
        s=EvidenceFixture(args,'partial-'+str(dim));state={}
        for ordinal in range(n):
            key=n//2-ordinal;g=98 if ordinal<10 else 99 if ordinal>=n-10 else ordinal%10
            state[key]=[g,normal(rng,dim),"boundary'中",ordinal%7]
        ids=list(state);prefix,suffix=ids[20],ids[-20]
        state[prefix][1]=[0.]*dim;state[suffix][1]=[0.]*dim
        try:
            s.start();load(s,state,dim);truth=Truth(state)
            def cycle():
                nonlocal total
                for k,c,project in [(10,[],False),(10,[('g','=',7)],False),(10,[('g','=',98)],False),(10,[('g','=',99)],False),(10,[('g','=',1000)],False),(10,[('g','>=',98)],True),(1,[],False),(64,[('g','=',7)],True),(1024,[],True)]:
                    v=[0.]*dim;total+=check(s,truth,v,c,k,project=v if project else None)
            # Cold filtered query creates a complete bitmap, then full-prefix
            # blocks become ready. Prefix-only/suffix-only predicates are new.
            total+=check(s,truth,normal(rng,dim),[('g','=',7)])
            cycle();cycle();s.shutdown();initial=diagnostics(s)
            if args.require_partial:
                b=initial['BLOCK_PATH'];assert b['builds']>0 and b['scans']>0 and 0<b['slots']<n and b['certified']==0,initial
            if env.get('MINIDB_PARTIAL_BLOCKS')=='0' and args.image and args.memory=='32m':assert initial['BLOCK_PATH']['builds']==0,initial
            if env.get('MINIDB_INDEX_MMAP')=='0' or env.get('MINIDB_SIMD')=='0':assert initial['BLOCK_PATH']['scans']==0,initial
            s.start();cycle()
            for ending in ('ROLLBACK','COMMIT'):
                saved=copy.deepcopy(state);q(s,'BEGIN')
                q(s,'UPDATE t SET g=99,v='+raw([0.]*dim)+f' WHERE id={prefix}');state[prefix][:2]=[99,[0.]*dim]
                q(s,f'DELETE FROM t WHERE id={suffix}');state.pop(suffix)
                q(s,f"INSERT INTO t VALUES ({suffix},98,'reinsert',"+raw([0.]*dim)+',3)');state[suffix]=[98,[0.]*dim,'reinsert',3]
                assert not s.request(f"INSERT INTO t VALUES (999999,99,'partial',"+raw([0.]*dim)+f",0),({prefix},99,'dup',"+raw([0.]*dim)+',0)')['ok'];assert q(s,'SELECT id FROM t WHERE id=999999')['rows']==[]
                truth=Truth(state);cycle();q(s,ending)
                if ending=='ROLLBACK':state=saved
                truth=Truth(state);cycle()
            # Change filter column and mix predicates so cached prefix grouping
            # cannot serve the query: a full scan/bitmap must cover both halves.
            for _ in range(2):
                for c in ([('h','=',3)],[('g','=',99),('h','>=',0)],[('g','=',98)],[('g','=',99)]):total+=check(s,truth,[0.]*dim,c)
            s.shutdown();s.start();cycle();s.shutdown()
            results[str(dim)]={'rows':n,'reverse_primary_keys':True,'prefix_tie_key':prefix,'suffix_tie_key':suffix,'initial_diagnostics':initial,'final_diagnostics':diagnostics(s),'transactions':['ROLLBACK','COMMIT','restart'],'full_truth_state_not_read_from_database':True}
        finally:s.close()
    return {'queries':total,'shapes':results}


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--binary');g.add_argument('--image');p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cpus',default='1');p.add_argument('--memory',default='32m');p.add_argument('--cgroupns',default=None);p.add_argument('--env',action='append',default=[]);p.add_argument('--section',choices=['strings','partial','all'],default='all');p.add_argument('--require-fast',action='store_true');p.add_argument('--require-partial',action='store_true');a=p.parse_args();a.env+=['MINIDB_POSTINGS=0'];a.output.mkdir(parents=True,exist_ok=False)
    r={'status':'RUNNING','host_only':bool(a.binary),'checks':{},'args':{k:str(v) if isinstance(v,Path) else v for k,v in vars(a).items()}}
    try:
        for name in ('strings','partial'):
            if a.section in (name,'all'):r['checks'][name]=globals()[name](a);print(name,'PASS',flush=True)
        r['status']='PASS'
    except BaseException as e:r.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
    finally:(a.output/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
