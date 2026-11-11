#!/usr/bin/env python3
"""Batch lookup equivalence across immutable segments, overlays and legacy keys."""
import json
import subprocess
from pathlib import Path

PROBE=r'''
import builtins,copy,json,math,random
from minidb.engine import Database
from minidb import storage
db=Database('/data')
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
q('CREATE TABLE t(c VARCHAR(32),v VECTOR(32),id BIGINT PRIMARY KEY)')
q('CREATE VECTOR INDEX ix ON t(v)')
ids=[-(1<<63),*range(-400,401),(1<<63)-1]
rng=random.Random(20261203);rng.shuffle(ids)
state={i:['中文🙂', [float(i%31)]*32,i] for i in ids}
for start in range(0,len(ids),60):
    q('INSERT INTO t VALUES '+','.join(f"('中文🙂','{json.dumps(state[i][1])}',{i})" for i in ids[start:start+60]))
checks=0
def lookup_check():
    global checks
    table=db.store.tables['t']
    wanted=[-(1<<63),(1<<63)-1,-400,-211,-3,0,1,6,280,400,999,0]
    expected={i:raw for i in set(wanted) if (raw:=db.store.lookup(table,i)[1]) is not None}
    assert dict(db.store.lookup_many(table,wanted))==expected
    assert not list(db.store.lookup_many(table,[]))
    checks+=2
def topk_check():
    global checks
    for k in (1,10,64,1000):
        expected=sorted((sum((v-3.)**2 for v in row[1]),i) for i,row in state.items())[:k]
        rows=q("SELECT id,L2_DISTANCE(v,'"+json.dumps([3.]*32)+"') AS d FROM t ORDER BY d LIMIT "+str(k))
        assert rows==[[i,math.sqrt(d)] for d,i in expected]
        checks+=1
lookup_check();topk_check()
before=copy.deepcopy(state)
q('BEGIN');q("UPDATE t SET v='"+json.dumps([0.]*32)+"' WHERE id=0");state[0][1]=[0.]*32
q('DELETE FROM t WHERE id=-400');state.pop(-400)
lookup_check();topk_check()
q('ROLLBACK');state=before;lookup_check();topk_check()
q('DELETE FROM t WHERE id=-400');state.pop(-400)
q("UPDATE t SET v='"+json.dumps([1.]*32)+"' WHERE id=0");state[0][1]=[1.]*32
lookup_check();topk_check()
# Verify generator close releases both handles while suspended at a row.
handles=[]
def tracked(*a,**kw):
    f=builtins.open(*a,**kw);handles.append(f);return f
storage.open=tracked
it=db.store.lookup_many(db.store.tables['t'],[-211,-3,1,6,280])
next(it);it.close()
del storage.open
assert handles and all(f.closed for f in handles)
assert len(handles)<=2*len(db.store.segments(db.store.tables['t']))
# Published legacy key-only files must retain exactly the same answers.
new=copy.deepcopy(db.store.tables['t'])
codec=storage.Codec(new['cols'])
for part in new['segments']:
    path=db.store.fresh('keys')
    with open(path,'wb') as f:
        f.write(storage.KEY_MAGIC)
        for ident,raw in storage.raw_rows(db.store.path(part['file']),codec,new['pk']):f.write(storage.I64.pack(ident))
    part['keys']=path.name
db.install('t',new);lookup_check();topk_check()
db.close();db=Database('/data');lookup_check();topk_check()
# Keyless original layout fallback also reads each segment only once.
for part in db.store.tables['t']['segments']:part.pop('keys',None)
lookup_check();topk_check();db.close()
print(json.dumps({'status':'PASS','checks':checks,'rows':803,'dimensions':32,'coverage':['batch vs single lookup','missing/duplicate/extreme IDs','multiple overlapping segments','non-leading primary key','Unicode rows','distance ties','K1/10/64/1000','transaction update/delete','rollback','committed overlay','early generator close','legacy key-only files','keyless files','restart']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=16m','--entrypoint','python3','minivectordb:lookup-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/lookup-boundaries';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
