#!/usr/bin/env python3
"""Independent SQL-level checks for the standalone runtime, on one frozen image.

--binary is a host development aid, never container/resource evidence.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
sys.path.insert(0, str(ROOT))
from _transport import Session
from minidb.sql import vector, DBError


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def identical(actual, expected):
    assert len(actual) == len(expected), (actual[:10], expected[:10])
    for a, b in zip(actual, expected):
        if isinstance(b, list):
            identical(a, b)
        elif isinstance(b, float):
            assert struct.pack('<d', a) == struct.pack('<d', b), (a, b)
        else:
            assert type(a) is type(b) and a == b, (a, b)


class CheckSession(Session):
    extra_env = ()

    def start(self, ready=True):
        if not self.extra_env or not self.image:
            return super().start(ready)
        image=self.image
        self.command=['docker','run','--name',self.name,'-i','--network=none',
            '--cpus='+self.config['cpus'],'--memory='+self.config['memory'],
            '--memory-swap='+self.config['memory'],'--pids-limit=128',
            '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
            '--mount',f'type=bind,src={self.bind_data.name},dst=/data']
        for item in self.extra_env:self.command.extend(['-e',item])
        self.command.append(image);self.image=None
        try:return super().start(ready)
        finally:self.image=image


class Target:
    def __init__(self, args):
        self.args = args
        self.config = json.loads((ROOT / 'protocol_check/config.json').read_text())
        self.config.update(cpus=args.cpus, memory=args.memory, case_timeout=3600)
        self.sequence = 0

    def session(self, label):
        self.sequence += 1
        s = CheckSession(self.config, self.args.output / f'{label}-{self.sequence}.stderr',
                    image=self.args.image,
                    command=[self.args.binary, '{data_dir}'] if self.args.binary else None,
                    data_root=str(self.args.output) if self.args.image else None)
        # Only this test's fresh directory; cap-drop=ALL is kept.
        if s.bind_data:
            os.chmod(s.bind_data.name, 0o777)
        s.extra_env = (['MINIDB_SIMD=0'] if self.args.scalar else []) + (['MINIDB_INDEX_MMAP=0'] if self.args.no_mmap else [])
        if self.args.binary and s.extra_env:s.command=['env']+s.extra_env+s.command
        return s


def ask(s, sql):
    result = s.request(sql)
    assert result['ok'], (sql[:200], result)
    return result


def numeric(target):
    s = target.session('numeric')
    count = 0
    try:
        s.start()
        ask(s, 'CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        numbers = ['0', '-0', '+0', '01', '.5', '1.', '1e3', '-1e-999', '1e309',
                   '1e39', '3.4028234663852886e38', '3.4028235677973366e38',
                   repr(2**-149), repr(2**-150), repr(math.nextafter(2**-150, math.inf)),
                   '١٢.٥', '１２.５']
        values = ['[' + sp + n + sp + ',-0]' for n in numbers
                  for sp in ('', ' ', '\t', '\n', '\x1c', '\u00a0', '\u2003')]
        values += ['[]', '[0]', '[0,0,0]', '[NaN,0]', '[Infinity,0]', '[true,0]',
                   '["1",0]', '[1,,2]', '[1e309,bad]', '[1e309]', '[0,]', '[1_0,0]']
        rng = random.Random(872909)
        for _ in range(300):
            raw = rng.randrange(0, 0x7f7ffffe)
            low, high = (struct.unpack('<f', struct.pack('<I', raw+i))[0] for i in (0, 1))
            mid = (low+high)/2
            for x in (math.nextafter(mid, 0.), mid, math.nextafter(mid, math.inf)):
                values.append(json.dumps([x, -x]))
        for i, text in enumerate(values):
            try:
                expected = vector(text, 2)
                error = None
            except DBError as e:
                error = e.code
            got = s.request(f'INSERT INTO t VALUES ({i},{literal(text)})')
            if error:
                assert not got['ok'] and got['error']['code'] == error, (text, error, got)
            else:
                assert got['ok'], (text, got)
                actual = ask(s, f'SELECT v FROM t WHERE id={i}')['rows'][0][0]
                assert struct.pack('<2f', *actual) == struct.pack('<2f', *expected), (text, actual, expected)
            count += 1
        for token in ('[-0,0]', "'[-0,0]'", '[-0.0,0]', '[-0e0,0]'):
            count += 1
            ask(s, f'INSERT INTO t VALUES ({10000+count},{token})')
            actual = ask(s, f'SELECT v FROM t WHERE id={10000+count}')['rows'][0][0]
            expected = 0. if token == '[-0,0]' else -0.
            assert struct.pack('<f', actual[0]) == struct.pack('<f', expected)
        s.shutdown()
    finally:
        s.close()
    return {'float32_bytes_and_error_codes': count}


def protocol(target):
    s=target.session('protocol');count=0
    try:
        s.start()
        for ident in (-(1<<80),1<<63,1<<80,'请求🙂',True,False,-0.0,1.5,[1,1<<80],{'n':1<<80}):
            result=s.request('COMMIT',ident=ident)
            assert result['ok'] and type(result['id']) is type(ident) and result['id']==ident
            if isinstance(ident,float):assert struct.pack('<d',result['id'])==struct.pack('<d',ident)
            result=s.request('UNSUPPORTED',ident=ident)
            assert not result['ok'] and type(result['id']) is type(ident) and result['id']==ident
            count+=2
        s.shutdown()
    finally:s.close()
    return {'typed_request_id_success_and_error':count}


def topk(target):
    rng = random.Random(9127)
    s = target.session('topk')
    checked = 0
    try:
        s.start()
        for dim in (16, 32, 128, 129, 256, 4096):
            for distribution in ('random', 'subnormal', 'large', 'ties'):
                name = f't{dim}{distribution}'
                ask(s, f'CREATE TABLE {name}(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR({dim}))')
                n = 137 if dim < 4096 else 35
                def value():
                    if distribution == 'subnormal': return rng.choice([0., -0., 2**-149, 2**-126])
                    if distribution == 'large': return f32(rng.uniform(-1e30, 1e30))
                    if distribution == 'ties': return 1.
                    return f32(rng.uniform(-8, 8))
                rows = {i-70: (i % 3, [value() for _ in range(dim)]) for i in range(n)}
                for start in range(0, n, 25):
                    batch = list(rows.items())[start:start+25]
                    ask(s, f'INSERT INTO {name} VALUES ' + ','.join(
                        f'({i},{c},{literal(json.dumps(v))})' for i,(c,v) in batch))
                ask(s, f'CREATE VECTOR INDEX ix ON {name}(v)')
                for qn in range(12):
                    q = [value() for _ in range(dim)]
                    eligible = [(i,v) for i,(c,v) in rows.items() if qn % 2 == 0 or c == qn % 3]
                    k = (1,10,127)[qn % 3]
                    truth = [[i, math.sqrt(d)] for d,i in sorted(
                        (math.fsum((a-b)*(a-b) for a,b in zip(v,q)), i) for i,v in eligible)[:k]]
                    expr = 'L2_DISTANCE(v,' + literal(json.dumps(q)) + ')'
                    sql = f'SELECT id,{expr} AS d FROM {name} ' + (f'WHERE c={qn%3} ' if qn%2 else '') + f'ORDER BY d LIMIT {k}'
                    identical(ask(s, sql)['rows'], truth)
                    checked += 1
        s.shutdown()
    finally:
        s.close()
    return {'bitwise_float64_topk_queries': checked}


def model(target):
    spec = importlib.util.spec_from_file_location('existing_model', ROOT / 'checks/model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.Session = lambda *a, **kw: target.session('model')
    previous = sys.argv
    sys.argv = ['model.py', '--output', str(target.args.output/'model')]
    try:
        module.main()
    finally:
        sys.argv = previous
    return {'operations': 250, 'independent_queries': 750}


def plans(target):
    s = target.session('plans')
    count = 0
    try:
        s.start()
        ask(s, 'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(2048),v VECTOR(2))')
        state = {i: [i%2, '界🙂'+str(i)+'x'*16, [float(i%17), float(i%11)]] for i in range(8194)}
        for start in range(0, len(state), 100):
            ask(s, 'INSERT INTO t VALUES ' + ','.join(
                f'({i},{c},{literal(text)},{literal(json.dumps(v))})'
                for i,(c,text,v) in list(state.items())[start:start+100]))
        ask(s, 'CREATE VECTOR INDEX ix ON t(v)')
        def verify():
            nonlocal count
            for q in ([0.,0.], [16.,10.]):
                for group in (None,0,1):
                    ranked = sorted((math.fsum((a-b)*(a-b) for a,b in zip(v,q)),i)
                                    for i,(c,text,v) in state.items() if group is None or c==group)[:10]
                    expr = 'L2_DISTANCE(v,'+literal(json.dumps(q))+')'
                    other = "L2_DISTANCE(v,'[1,1]')"
                    for projection in ('id,'+expr+' AS d', 'id,body,'+expr+' AS d', 'id,v,'+other+' AS other,'+expr+' AS d'):
                        truth=[]
                        for distance,i in ranked:
                            row=[i]
                            if 'body' in projection: row.append(state[i][1])
                            if ',v,' in projection: row += [state[i][2], math.sqrt(math.fsum((a-1)*(a-1) for a in state[i][2]))]
                            truth.append(row+[math.sqrt(distance)])
                        sql='SELECT '+projection+' FROM t '+('' if group is None else f'WHERE c={group} ')+f'ORDER BY d LIMIT 10'
                        identical(ask(s,sql)['rows'],truth); count+=1
            for lo,hi in ((-2,10),(4000,4100),(8190,9000),(-(1<<63),(1<<63)-1)):
                for reverse in (False,True):
                    ids=sorted((i for i in state if lo<=i<=hi),reverse=reverse)[:17]
                    sql=f'SELECT id,body FROM t WHERE id>={lo} AND id<={hi} ORDER BY id '+('DESC' if reverse else 'ASC')+' LIMIT 17'
                    identical(ask(s,sql)['rows'],[[i,state[i][1]] for i in ids]);count+=1
        verify(); saved=copy.deepcopy(state)
        ask(s,'BEGIN');ask(s,'DELETE FROM t WHERE id=3');del state[3]
        bad=s.request("INSERT INTO t VALUES (0,0,'dup','[0,0]')");assert not bad['ok']
        ask(s,"INSERT INTO t VALUES (3,0,'重插','[0,0]')");state[3]=[0,'重插',[0.,0.]]
        ask(s,"UPDATE t SET c=1,v='[16,10]' WHERE id=4");state[4][0]=1;state[4][2]=[16.,10.]
        verify();ask(s,'ROLLBACK');state=saved;verify()
        ask(s,'BEGIN');ask(s,'DELETE FROM t WHERE id=5');del state[5];ask(s,'COMMIT')
        s.crash();s.start();verify()
        # Empty mask accounting and cache churn. Changing query vectors cannot
        # change scalar predicate membership.
        for i in range(1200):
            assert ask(s,f"SELECT id FROM t WHERE c={i+100} ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10")['rows']==[]
        s.shutdown()
    finally:
        s.close()
    return {'projection_range_transaction_queries':count,'empty_predicate_churn':1200,
            'filter_boundary':'8194 rows / 4097 matches per class'}


def stress(target):
    s=target.session('stress');n=18000;text='x'*4096;v=[0.]*32
    try:
        s.start();ask(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(4096),v VECTOR(32))')
        for first in range(0,n,25):
            ask(s,'BEGIN');ask(s,'INSERT INTO t VALUES '+','.join(f'({i},{i%10},{literal(text)},{literal(json.dumps(v))})' for i in range(first,first+25)));ask(s,'COMMIT')
        ask(s,'CREATE VECTOR INDEX ix ON t(v)')
        ask(s,'BEGIN');assert ask(s,'UPDATE t SET c=7')['affected_rows']==n
        result=ask(s,'SELECT id,body FROM t ORDER BY c DESC,id DESC LIMIT 1100')['rows']
        assert result==[[i,text] for i in range(n-1,n-1101,-1)]
        ask(s,'ROLLBACK');assert ask(s,'SELECT id,c FROM t WHERE id=17')['rows']==[[17,7]]
        ask(s,'BEGIN');ask(s,'DELETE FROM t');s.crash();s.start()
        assert ask(s,'SELECT id,c FROM t ORDER BY id DESC LIMIT 2')['rows']==[[n-1,(n-1)%10],[n-2,(n-2)%10]]
        s.shutdown()
    finally:s.close()
    return {'rows':n,'logical_body_vector_bytes':n*(4096+32*4),'whole_table_update_external_sort_rollback_kill':True}


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--image');g.add_argument('--binary')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--cpus',default='1');p.add_argument('--memory',default='2g')
    p.add_argument('--section',choices=['protocol','numeric','topk','model','plans','stress','all'],default='all')
    p.add_argument('--scalar',action='store_true');p.add_argument('--no-mmap',action='store_true')
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    target=Target(args);report={'status':'RUNNING','target':args.image or args.binary,'host_only':bool(args.binary),'cpus':args.cpus,'memory':args.memory,'checks':{}}
    try:
        for name,fn in [('protocol',protocol),('numeric',numeric),('topk',topk),('model',model),('plans',plans),('stress',stress)]:
            if args.section in (name,'all'):
                report['active_check']=name;report['checks'][name]=fn(target);print(name,report['checks'][name],flush=True)
        report['status']='PASS'
    except BaseException as e:
        report['status']='FAIL';report['error']=repr(e);raise
    finally:
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
