#!/usr/bin/env python3
"""Native mapped reads across small-overlay merging and full mask compaction."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import heapq,json,math
from minidb.engine import Database
from minidb import storage
db=Database('/data');state={i:(0,float(i%17)) for i in range(1000)};checks=0
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(x) for x in r.encoded_rows]
    try:return [json.loads(x) for x in r.path.read_text().splitlines()]
    finally:r.path.unlink()
q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))')
q('INSERT INTO t VALUES '+','.join(f"({i},0,'{json.dumps([value]*128)}')" for i,(c,value) in state.items()))
q('CREATE VECTOR INDEX ix ON t(v)')
base=db.store.segments(db.store.tables['t'])[0]['file'];events={'small_merges':0,'fallbacks':0,'full_rewrites':0}
original_merge=db.store.merge_overlays;original_rewrite=db.store.rewrite
def merge(*a,**kw):
    result=original_merge(*a,**kw)
    events['small_merges' if result is not None else 'fallbacks']+=1
    return result
def rewrite(*a,**kw):events['full_rewrites']+=1;return original_rewrite(*a,**kw)
db.store.merge_overlays=merge;db.store.rewrite=rewrite
def check(value=0):
    global checks
    expected=heapq.nsmallest(10,((128*(v-value)**2,i,c) for i,(c,v) in state.items()))
    got=q("SELECT id,c,L2_DISTANCE(v,'"+json.dumps([float(value)]*128)+"') AS d FROM t ORDER BY d LIMIT 10")
    assert got==[[i,c,math.sqrt(d)] for d,i,c in expected]
    assert db.index_reader.maps and db.index_reader.bytes<=8<<20 and len(db.index_reader.maps)<=16
    checks+=1
check()
updates=storage.MAX_EXCLUDED+storage.MAX_SEGMENTS
assert updates<999
for i in range(updates):
    assert q("UPDATE t SET c="+str(1000+i)+",v='"+json.dumps([-1.]*128)+"' WHERE id="+str(i))['affected_rows']==1
    state[i]=(1000+i,-1.)
    assert not db.index_reader.maps
    if i==31:assert base in {p['file'] for p in db.store.segments(db.store.tables['t'])}
    check(-1 if i%2 else 0)
assert events['small_merges']>0 and events['full_rewrites']>0
saved=storage.MAX_SEGMENTS;storage.MAX_SEGMENTS=1
try:
    q("UPDATE t SET c=888,v='"+json.dumps([-3.]*128)+"' WHERE id=0");state[0]=(888,-3.)
    check(-3);assert events['fallbacks']>0
finally:storage.MAX_SEGMENTS=saved
before=state[999];q('BEGIN');q("UPDATE t SET c=777,v='"+json.dumps([-3.]*128)+"' WHERE id=999");state[999]=(777,-3.)
check(-3);q('ROLLBACK');state[999]=before;check(-1)
db.close();db=Database('/data');check(-1);db.close()
print(json.dumps({'status':'PASS','rows':1000,'dimensions':128,'sequential_updates':updates,'oracle_queries':checks,'events':events,'coverage':['mapped reads after each actual update','base retained at segment threshold','small overlay merge','full excluded-mask compaction','reduced-cap fallback to full rewrite','map invalidation','exact scalar/vector row consistency','transaction rollback','restart']}))
'''
r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=16m','--entrypoint','python3','minivectordb:mmap-merged','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
assert r.returncode==0,(r.stdout,r.stderr)
out=Path(__file__).resolve().parents[1]/'protocol-results/combined-overlays';out.mkdir(parents=True,exist_ok=True)
(out/'result.json').write_text(r.stdout);print(r.stdout,end='')
