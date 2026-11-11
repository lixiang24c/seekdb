#!/usr/bin/env python3
"""Actual-image bitmap reuse, view invalidation, eviction and oversized fallback."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import copy,json,math
from minidb import engine,native
from minidb.engine import Database
from minidb.sql import DBError
assert native.available()
db=Database('/data')
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
state={i-1500:[i%3,[float(i%17)]*128] for i in range(3000)}
for start in range(0,3000,100):
    q('INSERT INTO t VALUES '+','.join(f"({i},{state[i][0]},'{json.dumps(state[i][1])}')" for i in range(start-1500,start-1400)))
q('CREATE VECTOR INDEX ix ON t(v)')
checks=0
def check(category=1,query_value=0):
    global checks
    query=[float(query_value)]*128
    where='' if category is None else f'WHERE c={category} '
    truth=sorted((math.fsum((a-b)*(a-b) for a,b in zip(v,query)),i) for i,(c,v) in state.items() if category is None or c==category)[:10]
    assert q("SELECT id,L2_DISTANCE(v,'"+json.dumps(query)+"') AS d FROM t "+where+'ORDER BY d LIMIT 10')==[[i,math.sqrt(d)] for d,i in truth]
    checks+=1
for value in [0,1,2]:check(1,value)
assert db.filter_masks and db.filter_masks_bytes<=engine.MASK_BYTES
old_groups={id(entry[0]) for entry in db.filter_masks.values()}
check(1,3)
import os
assert all(ids is None or getattr(ids,'typecode',None)=='q' for ids,cost in db.filter_cache.values())
db.filter_cache.clear();db.filter_cache_bytes=0
os.environ['MINIDB_NATIVE']='0'
check(1,4)
assert db.filter_cache_bytes==0
del os.environ['MINIDB_NATIVE']
assert old_groups=={id(entry[0]) for entry in db.filter_masks.values()}
for category in [0,2]:check(category);check(category,1)
assert all(ids is None or getattr(ids,'typecode',None)=='q' for ids,cost in db.filter_cache.values())
original=copy.deepcopy(state)
q('BEGIN');q("UPDATE t SET c=1,v='"+json.dumps([0.]*128)+"' WHERE id=-1500");state[-1500]=[1,[0.]*128]
q('DELETE FROM t WHERE id=-1499');state.pop(-1499)
q("INSERT INTO t VALUES(4000,1,'"+json.dumps([0.]*128)+"')");state[4000]=[1,[0.]*128]
for value in [0,1,2]:check(1,value)
check(None)
try:q("INSERT INTO t VALUES(-1500,1,'"+json.dumps([0.]*128)+"')")
except DBError:pass
else:raise AssertionError('duplicate accepted')
check(1)
q('ROLLBACK');state=original
for value in [0,1,2]:check(1,value)
q('BEGIN');q('DELETE FROM t WHERE id=-1500');state.pop(-1500)
check(None);check(None,1)
q('COMMIT');check(1);check(1,1);check(1,2)
q('CREATE VECTOR INDEX ix2 ON t(v)');check(1)
# Exercise real builder overflow using a smaller test-only budget.
saved_budget=engine.MASK_BYTES
engine.MASK_BYTES=2048;db.filter_masks.clear();db.filter_masks_bytes=0
check(1);check(1,1)
assert any(masks is None for masks,cost in db.filter_masks.values())
assert db.filter_masks_bytes<=engine.MASK_BYTES
engine.MASK_BYTES=saved_budget
for i in range(10):db.remember_masks(('eviction',i),{'test-index':bytes(40000)})
assert db.filter_masks_bytes<=saved_budget and len(db.filter_masks)<=3
original_records,scans=db.store.records,[]
def counted_records(*args,**kwargs):
    scans.append(1)
    yield from original_records(*args,**kwargs)
db.store.records=counted_records
check(1);check(1,1)
assert not scans
db.store.records=original_records
assert db.filter_masks_bytes<=saved_budget
q('BEGIN');q('DELETE FROM t');state_before=state;state={};check(1)
q('ROLLBACK');state=state_before;check(1)
db.close();db=Database('/data')
check(1);check(1,1);check(1,2);db.close()
print(json.dumps({'status':'PASS','oracle_queries':checks,'rows':3000,'dimensions':128,'bitmap_budget':saved_budget,'reduced_budget_fallback':2048,'checks':['group reuse with different query vectors','compact int64 IDs survive bitmap eviction','native-off cached-mask fallback','multiple predicates without ID cache','transaction mutation visibility','failed statement','rollback','delete-only mask invalidation','second index','builder overflow fallback','LRU eviction without predicate rescan','delete all rollback','restart']}))
'''
r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=32m','--entrypoint','python3','minivectordb:bitmap-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
assert r.returncode==0,(r.stdout,r.stderr)
out=Path(__file__).resolve().parents[1]/'protocol-results/bitmap-boundaries';out.mkdir(parents=True,exist_ok=True)
(out/'result.json').write_text(r.stdout);print(r.stdout,end='')
