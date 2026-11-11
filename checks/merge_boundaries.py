#!/usr/bin/env python3
"""Verify fused and wide merge cascades, overlays and statement atomicity."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import copy,json,math,random
from minidb.engine import Database
from minidb.sql import DBError
db=Database('/data');checks=0
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
def batch(table,ids):
    return q('INSERT INTO '+table+' VALUES '+','.join(f"({i},'{json.dumps([float(i%17)]*32)}')" for i in ids))
def verify(table,state):
    global checks
    assert q('SELECT * FROM '+table+' ORDER BY id')==[[i,v] for i,v in sorted(state.items())]
    for mode in ('exact','ann'):
        r=db.execute("SELECT id FROM "+table+" ORDER BY L2_DISTANCE(v,'"+json.dumps([0.]*32)+"') LIMIT 10",mode)
        rows=[json.loads(row) for row in r.encoded_rows]
        truth=sorted((sum(x*x for x in v),i) for i,v in state.items())[:10]
        assert rows==[[i] for d,i in truth]
    checks+=3
q('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(32))');q('CREATE VECTOR INDEX ix ON t(v)')
ids=list(range(-500,500));random.Random(20261205).shuffle(ids)
state={}
for start in range(0,700,100):
    batch('t',ids[start:start+100]);state.update((i,[float(i%17)]*32) for i in ids[start:start+100])
ident=ids[0];q("UPDATE t SET v='"+json.dumps([0.]*32)+f"' WHERE id={ident}");state[ident]=[0.]*32
original=db.store.write_segment;writes=[]
def counted(*a,**kw):
    writes.append(1);return original(*a,**kw)
db.store.write_segment=counted
batch('t',ids[700:800]);state.update((i,[float(i%17)]*32) for i in ids[700:800])
assert len(writes)==1 and len(db.store.tables['t']['segments'])==1
fused_writes=len(writes);verify('t',state)
q('BEGIN');batch('t',[10000]);state[10000]=[float(10000%17)]*32
try:batch('t',[10001,ids[0]])
except DBError as e:assert e.code=='duplicate_key'
else:assert False
verify('t',state);q('COMMIT')
before=copy.deepcopy(state);q('BEGIN');batch('t',ids[800:900]);state.update((i,[float(i%17)]*32) for i in ids[800:900]);verify('t',state)
q('ROLLBACK');state=before;verify('t',state)
# Twenty overlay segments exceed the 16-reader direct merge limit.
q('CREATE TABLE u(id BIGINT PRIMARY KEY,v VECTOR(32))');q('CREATE VECTOR INDEX ix ON u(v)')
batch('u',range(100));other={i:[float(i%17)]*32 for i in range(100)}
for i in range(20):
    q("UPDATE u SET v='"+json.dumps([-1.]*32)+f"' WHERE id={i}");other[i]=[-1.]*32
writes.clear();batch('u',range(100,200));other.update((i,[float(i%17)]*32) for i in range(100,200))
wide_writes=len(writes);assert wide_writes>1
verify('u',other)
db.close();db=Database('/data');verify('t',state);verify('u',other);db.close()
print(json.dumps({'status':'PASS','oracle_queries':checks,'fused_segment_writes':fused_writes,'wide_fallback_segment_writes':wide_writes,'coverage':['overlapping key ranges','overlay visibility','one final run','indexed insert','duplicate statement atomicity','prior successful transaction statement','rollback','wide-cascade bounded fallback','distance ties','restart']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=16m','--entrypoint','python3','minivectordb:merge-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/merge-boundaries';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
