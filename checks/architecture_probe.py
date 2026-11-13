#!/usr/bin/env python3
"""Architecture migration checks; independent numeric/state oracles, no timing claims."""
import argparse
import collections
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from minidb import sql, native, indexio
from minidb.engine import Database, QueryResult
from minidb.native_storage import vector_parser
from minidb.query_native import NativeTopK

COUNTS = collections.Counter()

def bits(value):
    if isinstance(value, float): return ('f64', struct.pack('<d', value).hex())
    if isinstance(value, (list, tuple)): return [bits(x) for x in value]
    if isinstance(value, dict): return {k: bits(v) for k, v in value.items()}
    return value

def execute(db, text):
    try:
        result = db.execute(text)
        if hasattr(result, 'columns'):
            if result.path is None: result = [json.loads(x) for x in result.encoded_rows]
            else:
                path = result.path
                try: result = [json.loads(x) for x in path.read_text().splitlines()]
                finally: path.unlink()
        return ('ok', bits(result))
    except Exception as error:
        if not hasattr(error, 'code'): raise
        return ('error', error.code, str(error))

def reference(path):
    spec = importlib.util.spec_from_file_location('baseline_db', path/'minidb/__init__.py',
                                                submodule_search_locations=[str(path/'minidb')])
    module = importlib.util.module_from_spec(spec);sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    import baseline_db.engine, baseline_db.sql
    return baseline_db.engine.Database, baseline_db.sql

def numeric(ref):
    numbers = ['0','-0','+0','01','-00','.5','1.','1e3','-1e-999','1e309','1e39',
        '3.4028234663852886e38','3.4028235677973366e38',repr(2**-149),repr(2**-150),
        repr(math.nextafter(2**-150, math.inf)),'١٢.٥','１２.５']
    spaces = ['', ' ', '\t', '\n', '\r', '\v', '\f', '\x1c', '\u00a0', '\u2003']
    cases = [('['+s+n+s+','+s+'-0'+s+']',d) for n,s,d in itertools.product(numbers, spaces, (1,2,3))]
    cases += [('['+n+']',d) for n,d in itertools.product(
        ['nan','Infinity','true','false','null','"1"','[]','{}','1e','0x1','1_0','','1e309,bad','1,,2'], (0,1,2))]
    class Int(int): pass
    for value in ([True],['1'],[None],[Int(1)],[math.inf],[math.nan],[10**1000],[],[0.,-0.,2**-149],(0,1),None,b'[1]'):
        cases.extend((value,d) for d in (0,1,2,3))
    rng=random.Random(8911)
    for _ in range(1000):
        raw=rng.getrandbits(32); v=struct.unpack('<f',struct.pack('<I',raw))[0]
        if not math.isfinite(v):continue
        cases.append((json.dumps([v, -0.]),2))
    for _ in range(100):
        raw=rng.randrange(0,0x7f7ffffe)
        low,high=(struct.unpack('<f',struct.pack('<I',raw+i))[0] for i in (0,1))
        midpoint=(low+high)/2
        for value in (math.nextafter(midpoint,0.),midpoint,math.nextafter(midpoint,math.inf)):
            cases.append((json.dumps([value,-value]),2))
    for n in (0,1,4095,4096,4097):
        cases.extend(('['+','.join(['-0']*n)+']',d) for d in (n,n+1))
    for length in (65535,65536,65537):cases.append(('['+' '*(length-4)+'-0]',1))
    def result(module,value,dim,compact=False):
        try:
            values=module.vector(value,dim,compact=True) if compact else module.vector(value,dim)
            return ('bits',struct.pack('<'+str(len(values))+'f',*values).hex())
        except module.DBError as e:return ('error',e.code,str(e))
    for value,dim in cases:
        expected=result(ref,value,dim)
        for compact in (False,True):
            actual=result(sql,value,dim,compact)
            assert actual==expected, (repr(value)[:200],dim,actual,expected)
            COUNTS['numeric_bytes_errors']+=1
    assert vector_parser.parsed_vectors>0

def topk():
    rng=random.Random(44221)
    for dim in (16,17,32,128,256,4096):
        def finite():
            while True:
                v=struct.unpack('<f',struct.pack('<I',rng.getrandbits(32)))[0]
                if math.isfinite(v):return v
        for distribution in ('bits','near','zero','midpoint'):
            n=137 if dim<4096 else 35
            vectors=[[finite() if distribution=='bits' else rng.choice([1.,math.nextafter(1.,2.),1.+2**-23]) if distribution=='near' else rng.choice([0.,-0.,2**-149]) for _ in range(dim)] for i in range(n)]
            if distribution=='midpoint':vectors=[[1.]+[2**-27]*(i%8)+[0.]*(dim-1-i%8) for i in range(n)]
            # Inputs are actual float32 values, like persisted rows.
            fmt=struct.Struct('<q'+str(dim)+'f')
            ids=[-(1<<63), (1<<63)-1]+list(range(-n, -2))
            ids=ids[:n]
            q=[finite() if distribution=='bits' else 0. for _ in range(dim)]
            raw=[fmt.pack(i,*v) for i,v in zip(ids,vectors)]
            vectors=[fmt.unpack(r)[1:] for r in raw]
            for filtered in (False,True):
                for k in (1,10,n+5):
                    ctx=NativeTopK(native.Screen(q),k);assert ctx.handle
                    try:
                        for start in range(0,n,13):
                            block=b''.join(raw[start:start+13]);mask=bytes(int((start+j)%3==0) for j in range(len(block)//fmt.size)) if filtered else None
                            ctx.scan(block,mask,None)
                        actual=sorted((d,i) for i,d in ctx.scores().items())
                        expected=sorted((math.fsum((x-y)*(x-y) for x,y in zip(v,q)),i) for j,(i,v) in enumerate(zip(ids,vectors)) if not filtered or j%3==0)[:k]
                        assert bits(actual)==bits(expected),(dim,distribution,filtered,k,actual[:2],expected[:2])
                        COUNTS['cross_block_exact_topk']+=1
                    finally:ctx.close()

def database(baseline):
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        db=Database(a);ref=baseline(b)
        def check(text):
            actual=execute(db,text);expected=execute(ref,text)
            assert actual==expected,(text,actual,expected)
            assert execute(db,'SELECT * FROM t ORDER BY id')==execute(ref,'SELECT * FROM t ORDER BY id'),text
            COUNTS['statement_and_full_state']+=1
        try:
            check('CREATE TABLE t(v VECTOR(2),s VARCHAR(8),id BIGINT PRIMARY KEY,c BIGINT)')
            check("INSERT INTO t VALUES ([-0,-0e0],'界',-9,1),('[0,-0]','a',2,2)")
            check("INSERT INTO t (`c`,id,`s`,v) VALUES (0,3,'a''b','[.5,+1]')")
            check('CREATE VECTOR INDEX ix ON t(v)')
            for ending in ('COMMIT','ROLLBACK'):
                check('BEGIN')
                for text in ["INSERT INTO t VALUES ('[1,2]','x',7,0),('[0,0]','dup',2,1)",
                    "INSERT INTO t VALUES ('[1,2]','x',7,0),('[1e309,0]','bad',8,0)",
                    "INSERT INTO t VALUES ('[1,2]','x',7,0),('[0]','dim',8,0)",
                    "INSERT INTO t VALUES ('[1,2]','x',7,0),('[0,0]','toolongxxx',8,0)",
                    "INSERT INTO t VALUES ('[1,2]','x',7,0),('[0,0]','bad',8,1.0)",
                    "INSERT INTO t VALUES ('[1,2]','x',7,0),('[0,0]','dup',7,0)",
                    "INSERT INTO t VALUES ('[0,0]','end',7,0); junk",
                    "INSERT INTO t VALUES ('[0,0]','end',7,0) /*fallback*/",
                    "UPDATE t SET v='[-0,3]',c=5 WHERE id=2",'DELETE FROM t WHERE id=-9',
                    "INSERT INTO t VALUES ('[-0,-0]','new',-9,7)",
                    "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE c>=1 ORDER BY d LIMIT 10"]:check(text)
                check(ending)
                db.close();ref.close();db=Database(a);ref=baseline(b)
                check('SELECT * FROM t ORDER BY id')
            # Different vector positions, signed primary keys, scalar-only and full projections.
            for pos in (0,1,2):
                cols=['id BIGINT PRIMARY KEY','s VARCHAR(20)'];cols.insert(pos,'v VECTOR(2)')
                check('CREATE TABLE u'+str(pos)+'('+','.join(cols)+')')
                values=[str(-(1<<63)),"'尾' "];values.insert(pos,"'[-0,1]' ")
                check('INSERT INTO u'+str(pos)+' VALUES ('+','.join(values)+')')
                check('SELECT * FROM u'+str(pos))
            assert (db.store.cpp.prepared_inserts>0)==(os.environ.get('MINIDB_NATIVE')!='0')
            COUNTS['native_batches']+=db.store.cpp.prepared_inserts
            COUNTS['native_merges']+=db.store.cpp.merges
        finally:db.close();ref.close()

def mixed(baseline):
    with tempfile.TemporaryDirectory() as root:
        old=baseline(root);execute(old,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(16))')
        state={i:[float(i%17)]*16 for i in range(800)}
        literal=lambda i:json.dumps(state[i])
        for start in range(0,400,100):
            assert execute(old,'INSERT INTO t VALUES '+','.join(f"({i},0,'{literal(i)}')" for i in range(start,start+100)))[0]=='ok'
        execute(old,'CREATE VECTOR INDEX ix ON t(v)');old.close()
        db=Database(root)
        try:
            for start in range(400,800,100):
                # Alternating Python/C++ columnar writers; old segments force the reference reader.
                os.environ['MINIDB_CPP_STORAGE']=str((start//100)%2)
                assert execute(db,'INSERT INTO t VALUES '+','.join(f"({i},0,'{literal(i)}')" for i in range(start,start+100)))[0]=='ok'
                actual=execute(db,'SELECT id,v FROM t ORDER BY id')
                assert actual==('ok',bits([[i,state[i]] for i in range(start+100)]))
                if start==400:
                    formats={bool(part.get('columnar')) for part in db.store.segments(db.store.tables['t'])}
                    assert formats=={False,True},'Fixture must actually mix old/new formats'
                COUNTS['legacy_and_native_mixed_writes']+=1
            os.environ.pop('MINIDB_CPP_STORAGE',None)
            execute(db,'BEGIN');execute(db,'DELETE FROM t WHERE id=1');execute(db,"INSERT INTO t VALUES (1,2,'"+json.dumps([0.]*16)+"')");execute(db,'ROLLBACK')
            db.close();db=Database(root)
            assert execute(db,'SELECT id,v FROM t ORDER BY id')==('ok',bits([[i,state[i]] for i in range(800)]))
            COUNTS['mixed_restart']+=1
        finally:db.close();os.environ.pop('MINIDB_CPP_STORAGE',None)

def va():
    saved=indexio.file_cache_budget
    with tempfile.TemporaryDirectory() as root:
        db=Database(root);rows={i:([float(i)]*16,i//1024) for i in range(2048)}
        try:
            assert execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(16))')[0]=='ok'
            assert execute(db,'INSERT INTO t VALUES '+','.join(f"({i},{c},'{json.dumps(v)}')" for i,(v,c) in rows.items()))[0]=='ok'
            execute(db,'CREATE VECTOR INDEX ix ON t(v)')
            os.environ['MINIDB_VA']='1'
            for query in (0.,1023.,2047.):
                for clause in ('',' WHERE c=0',' WHERE c=1',' WHERE c=9'):
                    expected=sorted((math.fsum((x-query)**2 for x in v),i) for i,(v,c) in rows.items() if not clause or c==int(clause[-1]))[:10]
                    text=f"SELECT id,L2_DISTANCE(v,'{json.dumps([query]*16)}') AS d FROM t{clause} ORDER BY d LIMIT 10"
                    for _ in range(2):
                        assert execute(db,text)==('ok',bits([[i,math.sqrt(d)] for d,i in expected]))
                        COUNTS['va_mask_oracle']+=1
            assert db.approx.hits>0 and NativeTopK.files>0
            COUNTS['va_hits']+=db.approx.hits
            # Unknown budget must not admit VA; streaming remains bounded.
            os.environ.pop('MINIDB_VA',None);db.approx.clear();db.index_reader.clear()
            indexio.file_cache_budget=lambda:None
            execute(db,"SELECT id FROM t ORDER BY L2_DISTANCE(v,'"+json.dumps([0.]*16)+"') LIMIT 10")
            assert not db.approx.entries and db.index_reader.streaming
            COUNTS['unknown_budget_fallback']+=1
        finally:db.close();indexio.file_cache_budget=saved;os.environ.pop('MINIDB_VA',None)

    # Exercise real automatic admission, not just the diagnostic force switch.
    # Continuous float32 data, index > visible cache budget, independent oracle.
    from array import array
    rng=random.Random(90271)
    rows=[array('f',(rng.uniform(-8,8) for _ in range(128))) for _ in range(8000)]
    indexio.file_cache_budget=lambda:2<<20
    with tempfile.TemporaryDirectory() as root:
        db=Database(root)
        try:
            execute(db,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
            for start in range(0,len(rows),100):
                assert execute(db,'INSERT INTO t VALUES '+','.join(f"({i-4000},{i%10},'{json.dumps(list(rows[i]))}')" for i in range(start,start+100)))[0]=='ok'
            execute(db,'CREATE VECTOR INDEX ix ON t(v)')
            for q in (rows[0],rows[501],array('f',(rng.uniform(-8,8) for _ in range(128)))):
                for category in (None,1):
                    where='' if category is None else ' WHERE c=1'
                    expected=sorted((math.fsum((x-y)*(x-y) for x,y in zip(v,q)),i-4000) for i,v in enumerate(rows) if category is None or i%10==category)[:10]
                    text=f"SELECT id,L2_DISTANCE(v,'{json.dumps(list(q))}') AS d FROM t{where} ORDER BY d LIMIT 10"
                    assert execute(db,text)==('ok',bits([[i,math.sqrt(d)] for d,i in expected]))
                    COUNTS['auto_va_continuous_oracle']+=1
            assert db.index_reader.streaming and db.approx.builds>0
            COUNTS['auto_va_builds']+=db.approx.builds
            COUNTS['auto_va_hits']+=db.approx.hits
            COUNTS['auto_va_scan_fallbacks']+=db.approx.fallbacks
        finally:db.close();indexio.file_cache_budget=saved

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--section',choices=['numeric','topk','database','mixed','va','all'],default='all');args=p.parse_args()
    base,ref=reference(args.baseline)
    for name,fn in [('numeric',lambda:numeric(ref)),('topk',topk),('database',lambda:database(base)),('mixed',lambda:mixed(base)),('va',va)]:
        if args.section in (name,'all'):fn()
    print(json.dumps({'status':'PASS','checks':COUNTS,'native':{'parsed_vectors':vector_parser.parsed_vectors,'scans':NativeTopK.scans,'candidate_files':NativeTopK.files},'simd':native._library.mv_screen_simd() if native.available() else None}))
if __name__=='__main__':main()
