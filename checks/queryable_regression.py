#!/usr/bin/env python3
"""Queryable-log gates. Host runs are not cgroup/performance evidence."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import sys
import traceback

import native_journal as journal
from native_validation import identical, f32


class Fixture(journal.Fixture):
    def __init__(self,args,label):
        super().__init__(args,label)
        env=['MINIDB_DIAGNOSTICS=1']+args.env
        if args.image:
            at=len(self.command)-1
            self.command[at:at]=[x for item in env for x in ('-e',item)]
            if args.cgroupns:self.command[2:2]=['--cgroupns='+args.cgroupns]
        else:self.command[1:1]=env
        self.error_path=args.output/(label+'.stderr')

    def diagnostics(self):
        lines=self.error_path.read_text().splitlines()
        return json.loads(next(x.split(' ',1)[1] for x in reversed(lines) if x.startswith('MINIDB_PATH ')))


q=journal.q


def recover(s,expected):
    for _ in range(2):
        s.start();identical(journal.rows(s),expected);s.shutdown()
    assert not (s.root/'queryable.next').exists()
    assert not list(s.root.glob('query-result-*'))


def faults(args):
    results=[]
    for variant in ('auto','single','multi'):
        # A directory-only COMMIT has header/metadata/trailer (three writes).
        points=['write-1','write-2','write-3']+(['write-4'] if variant=='auto' else [])+['sync-before','sync-after','sync-eio']
        for point in points:
            s=Fixture(args,variant+'-'+point)
            before=[[1,1,[3.,4.]]];after=before+[[2,2,[5.,6.]]]
            insert="INSERT INTO t VALUES (2,2,'"+'x'*512+"','[5,6]')"
            try:
                journal.setup(s)
                if variant!='auto':
                    q(s,'BEGIN');q(s,insert)
                    assert not s.request("INSERT INTO t VALUES (1,9,'duplicate','[0,0]')")['ok']
                    if variant=='multi':q(s,'UPDATE t SET c=7 WHERE id=1');after[0]=[1,7,[3.,4.]]
                    identical(journal.rows(s),after)
                s.arm(point);s.request(insert if variant=='auto' else 'COMMIT',wait=False)
                if point=='sync-eio':
                    try:s.read(30)
                    except journal.ProtocolError:pass
                    else:raise AssertionError('sync failure acknowledged')
                    s.proc.wait(timeout=30);assert s.proc.returncode==1
                    assert 'sync queryable commit: Input/output error' in s.error_path.read_text()
                    s.stop();s.disarm()
                else:s.kill_at_marker(point)
                s.start();actual=journal.rows(s);assert actual in ([after] if point=='sync-after' else [before,after]);s.shutdown()
                recover(s,actual)
                results.append({'variant':variant,'point':point,'status':'PASS'})
            finally:s.close()
    for point in ('checkpoint-write','checkpoint-sync','checkpoint-before-rename','checkpoint-after-rename','checkpoint-directory'):
        s=Fixture(args,point)
        try:
            journal.setup(s);q(s,'CREATE TABLE wide(id BIGINT PRIMARY KEY,body VARCHAR(32768))')
            for start in range(0,80,8):q(s,'INSERT INTO wide VALUES '+','.join(f"({i},'"+'x'*32768+"')" for i in range(start,start+8)))
            s.arm(point);s.request('DELETE FROM wide',wait=False);s.kill_at_marker(point)
            for _ in range(3):
                s.arm('recover-read');s.start(ready=False);s.kill_at_marker('recover-read')
            for _ in range(2):
                s.start();assert q(s,'SELECT id FROM wide')['rows']==[]
                identical(journal.rows(s),[[1,1,[3.,4.]]]);s.shutdown()
                assert not (s.root/'queryable.next').exists()
            results.append({'point':point,'recovery_interruptions':3,'status':'PASS'})
        finally:s.close()
    s=Fixture(args,'tails-cleanup')
    try:
        journal.setup(s);s.shutdown();path=s.root/'queryable.log';original=path.read_bytes()
        for tail in (b'partial',struct.pack('<QI',1000,500)+b'partial'):
            journal.replace_fixture_file(s,path,original+tail)
            (s.root/'queryable.next').write_bytes(b'orphan')
            (s.root/'query-result-orphan').write_bytes(b'orphan')
            recover(s,[[1,1,[3.,4.]]]);assert path.read_bytes()==original
        results.append({'partial_tail_and_orphan_cleanup':'PASS'})
    finally:s.close()
    return results


def transactions(args):
    s=Fixture(args,'long-transactions');state={};checkpoints=0
    try:
        s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(8192),v VECTOR(2))')
        # More than four shared directory pages; failures do not abort the transaction.
        for ending in ('COMMIT','ROLLBACK','KILL'):
            q(s,'BEGIN');visible=dict(state)
            for i in range(300):
                key=(0 if ending=='COMMIT' else 1000)+i
                q(s,f"INSERT INTO t VALUES ({key},{i%7},'x',[{i%13},{i%11}])")
                visible[key]=[i%7,[float(i%13),float(i%11)]]
                if i%31==0:
                    assert not s.request(f"INSERT INTO t VALUES ({key+10000},0,'ok',[0,0]),({key},0,'dup',[0,0])")['ok']
                    assert q(s,f'SELECT id FROM t WHERE id={key+10000}')['rows']==[]
            expected=[[i,*v] for i,v in sorted(visible.items())]
            identical(q(s,'SELECT id,c,v FROM t ORDER BY id')['rows'],expected)
            q(s,'DELETE FROM t WHERE id=5');visible.pop(5,None)
            q(s,"INSERT INTO t VALUES (5,9,'again',[2,3])");visible[5]=[9,[2.,3.]]
            if ending=='KILL':s.crash();s.start()
            else:q(s,ending)
            if ending=='COMMIT':state=visible
            identical(q(s,'SELECT id,c,v FROM t ORDER BY id')['rows'],[[i,*v] for i,v in sorted(state.items())])
        previous=(s.root/'queryable.log').stat().st_size
        for turn in range(20):
            q(s,"UPDATE t SET body='"+('x'*8192)+f"',c={turn}")
            for i in state:state[i][0]=turn
            size=(s.root/'queryable.log').stat().st_size
            checkpoints+=size<previous;previous=size
            if turn%4==0:
                s.crash();s.start()
                identical(q(s,'SELECT id,c,v FROM t ORDER BY id')['rows'],[[i,*v] for i,v in sorted(state.items())])
        assert checkpoints>0,'no reclamation was observed'
        q(s,'DELETE FROM t');assert q(s,'SELECT id FROM t')['rows']==[]
        assert (s.root/'queryable.log').stat().st_size<1<<20
        s.shutdown();assert not list(s.root.glob('query-result-*'))
        return {'long_transactions':3,'statements_per_transaction':300,'update_cycles':20,'observed_log_shrinks':checkpoints}
    finally:s.close()


def paths(args):
    s=Fixture(args,'paths');rng=random.Random(42);n=1152;dim=128
    state={i:[i%10,[f32(rng.gauss(0,1)) for _ in range(dim)]] for i in range(n)}
    count=0
    try:
        s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
        for start in range(0,n,64):q(s,'INSERT INTO t VALUES '+','.join(f'({i},{state[i][0]},{json.dumps(state[i][1])})' for i in range(start,min(n,start+64))))
        q(s,'CREATE VECTOR INDEX ix ON t(v)')
        def verify():
            nonlocal count
            for j in range(16):
                vector=[f32(rng.gauss(0,1)) for _ in range(dim)] if j<12 else [0.]*dim
                group=0 if j%2 else None;k=(1,10,64,1024)[j%4]
                expected=[[i,math.sqrt(d)] for d,i in sorted((math.fsum((x-y)*(x-y) for x,y in zip(v,vector)),i) for i,(c,v) in state.items() if group is None or c==group)[:k]]
                expr='L2_DISTANCE(v,'+json.dumps(vector)+')'
                sql='SELECT id,'+expr+' AS d FROM t '+('WHERE c=0 ' if group is not None else '')+f'ORDER BY d LIMIT {k}'
                identical(q(s,sql)['rows'],expected);count+=1
            # Different predicates on one column exercise dictionary reuse,
            # rather than hitting only the already-cached bitmap.
            for group in (1,2,3):
                vector=[0.]*dim
                expected=[[i,math.sqrt(d)] for d,i in sorted((math.fsum(x*x for x in v),i) for i,(c,v) in state.items() if c==group)[:10]]
                identical(q(s,'SELECT id,L2_DISTANCE(v,'+json.dumps(vector)+f') AS d FROM t WHERE c={group} ORDER BY d LIMIT 10')['rows'],expected);count+=1
        verify();saved={i:[c,list(v)] for i,(c,v) in state.items()}
        for ending in ('ROLLBACK','COMMIT'):
            q(s,'BEGIN');q(s,'DELETE FROM t WHERE id=0');state.pop(0)
            zero=[0.]*dim;q(s,'INSERT INTO t VALUES (0,0,'+json.dumps(zero)+')');state[0]=[0,zero]
            q(s,'UPDATE t SET c=0,v='+json.dumps(zero)+' WHERE id=1');state[1]=[0,zero]
            assert not s.request('INSERT INTO t VALUES (0,0,'+json.dumps(zero)+')')['ok']
            verify();q(s,ending)
            if ending=='ROLLBACK':state={i:[c,list(v)] for i,(c,v) in saved.items()}
            verify()
        s.crash();s.start();verify();s.shutdown();d=s.diagnostics()
        # Counters cover the final restarted process, not the earlier killed one.
        env=dict(x.split('=',1) for x in args.env)
        if env.get('MINIDB_INDEX_MMAP')=='0':assert d['mapped_opens']==d['posting_scans']==d['quant_scan_calls']==0 and d['buffered_scans']>0
        if env.get('MINIDB_SIMD')=='0':assert d['simd']==0 and d['quant_scan_calls']==0
        if env.get('MINIDB_QUANT_SCREEN')=='0' or env.get('MINIDB_FLOAT_SCREEN')=='0' or d['budget_bytes']<64<<20:assert d['quant_builds']==d['quant_scan_calls']==0
        expected_postings=getattr(args,'expected_postings',None)
        if expected_postings is not None and 'MINIDB_POSTINGS' in env:
            assert expected_postings==env['MINIDB_POSTINGS'],'contradictory postings test configuration'
        if (expected_postings if expected_postings is not None else env.get('MINIDB_POSTINGS'))=='0':assert d['posting_scans']==0
        elif d['mapped_opens']:assert d['posting_scans']>0
        if d['budget_bytes']>=1<<20:assert d['integer_hits']>0
        if env.get('MINIDB_COMPACT_VECTOR')=='0':assert d['compact_vectors']==0
        else:assert d['compact_vectors']>0
        assert d['quant_peak_bytes']<=min(4<<20,d['budget_bytes']//32)
        return {'independent_bitwise_queries':count,'diagnostics':d,'env':args.env}
    finally:s.close()


def large(args):
    s=Fixture(args,'large');n=36000;dim=128
    # 18.4 MB of original float32 alone exceeds the 16 MiB test limit.
    def vec(i):return [float((i*17+j*13)%257-128)/16 for j in range(dim)]
    try:
        s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(2048),v VECTOR(128))')
        for start in range(0,n,100):q(s,'INSERT INTO t VALUES '+','.join(f"({i},{i%10},'"+'x'*2048+"',"+json.dumps(vec(i))+')' for i in range(start,min(n,start+100))))
        q(s,'CREATE VECTOR INDEX ix ON t(v)')
        queries=0
        for restart in range(2):
            for shift in range(4):
                v=[f32(x+.01) for x in vec(shift)];ids=range(shift%10,n,10) if shift%2 else range(n)
                truth=[[i,math.sqrt(d)] for d,i in sorted((math.fsum((x-y)*(x-y) for x,y in zip(vec(i),v)),i) for i in ids)[:10]]
                sql='SELECT id,L2_DISTANCE(v,'+json.dumps(v)+') AS d FROM t '+(f'WHERE c={shift%10} ' if shift%2 else '')+'ORDER BY d LIMIT 10'
                identical(q(s,sql)['rows'],truth);queries+=1
            s.crash();s.start()
        q(s,'BEGIN');q(s,'UPDATE t SET c=7 WHERE id<2000')
        assert not s.request("INSERT INTO t VALUES (0,0,'duplicate',"+json.dumps(vec(0))+')')['ok']
        q(s,'ROLLBACK');assert q(s,'SELECT id,c FROM t WHERE id=17')['rows']==[[17,7]]
        assert q(s,'SELECT id,c FROM t WHERE id=18')['rows']==[[18,8]]
        identical(q(s,sql)['rows'],truth);queries+=1
        size=(s.root/'queryable.log').stat().st_size;s.shutdown()
        diagnostics=s.diagnostics();assert diagnostics['mapped_opens']==0 and diagnostics['buffered_scans']>0
        return {'rows':n,'vector_bytes':n*dim*4,'log_bytes':size,'independent_queries':queries,'host_only':bool(args.binary),'diagnostics':diagnostics}
    finally:s.close()


def adversarial(args):
    s=Fixture(args,'adversarial');rng=random.Random(44);checked=0;n=1152;dim=128
    try:
        s.start()
        for shape in ('zero','ties','near','subnormal','large','mixed'):
            def value(i,j):
                if shape=='zero':return -0. if j%2 else 0.
                if shape=='ties':return 1.
                if shape=='near':return f32(1.+rng.randrange(-4,5)*2**-23)
                if shape=='subnormal':return rng.choice([0.,-0.,2**-149,2**-126])
                if shape=='large':return f32(rng.uniform(-1e30,1e30))
                return f32(rng.uniform(-1,1)*10.**rng.randrange(-30,31))
            state={i:[value(i,j) for j in range(dim)] for i in range(n)}
            q(s,f'CREATE TABLE {shape}(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
            for start in range(0,n,64):q(s,f'INSERT INTO {shape} VALUES '+','.join(f'({i},{i%10},{json.dumps(state[i])})' for i in range(start,min(n,start+64))))
            for query_number in range(8):
                vector=[value(-1,j) for j in range(dim)];k=(1,10,64,1024)[query_number%4]
                ids=range(query_number%10,n,10) if query_number%2 else range(n)
                truth=[[i,math.sqrt(d)] for d,i in sorted((math.fsum((x-y)*(x-y) for x,y in zip(state[i],vector)),i) for i in ids)[:k]]
                sql=f'SELECT id,L2_DISTANCE(v,{json.dumps(vector)}) AS d FROM {shape} '+(f'WHERE c={query_number%10} ' if query_number%2 else '')+f'ORDER BY d LIMIT {k}'
                identical(q(s,sql)['rows'],truth);checked+=1
        s.shutdown();return {'shapes':6,'rows_per_shape':n,'bitwise_topk_queries':checked}
    finally:s.close()


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--image');g.add_argument('--binary')
    p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--cpus',default='1');p.add_argument('--memory',default='384m');p.add_argument('--cgroupns',choices=['private','host'])
    p.add_argument('--expected-postings',choices=['0','1'],help='Assert the inherited image default without overriding its environment.')
    p.add_argument('--env',action='append',default=[]);p.add_argument('--section',choices=['faults','transactions','paths','large','adversarial','all'],default='all')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','host_only':bool(args.binary),'image':args.image,'checks':{},'args':{k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}}
    try:
        for name,fn in [('faults',faults),('transactions',transactions),('paths',paths),('large',large),('adversarial',adversarial)]:
            if args.section in (name,'all'):
                report['active']=name;report['checks'][name]=fn(args);print(name,'PASS',flush=True)
        report['status']='PASS'
    except BaseException as e:report.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
    finally:(args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
