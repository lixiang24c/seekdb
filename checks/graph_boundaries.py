#!/usr/bin/env python3
"""Actual-image graph use, exact oracle, view invalidation and restart."""
import json
import argparse
import subprocess
from pathlib import Path

PROBE = r'''
import json,math,random,struct
from minidb.engine import Database
db=Database('/data')
assert db.graphs.enabled and db.graphs.lib and db.graphs.ef==512
def q(sql,mode='exact'):
    r=db.execute(sql,mode)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
rng=random.Random(20261131)
f32=lambda x:struct.unpack('<f',struct.pack('<f',x))[0]
state={i-1024:[f32(rng.uniform(-1,1)) for _ in range(32)] for i in range(2048)}
q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(32))')
for start in range(-1024,1024,100):
    q('INSERT INTO t VALUES '+','.join(f"({i},{i%3},'{json.dumps(state[i])}')" for i in range(start,min(1024,start+100))))
q('CREATE VECTOR INDEX ix ON t(v)')
assert db.graphs.handle
checks=0;recalls=[]
def check(vec,mode='exact',filtered=False):
    global checks
    truth=sorted((math.fsum((a-b)**2 for a,b in zip(v,vec)),i) for i,v in state.items() if not filtered or i%3==1)[:10]
    sql="SELECT id,L2_DISTANCE(v,'"+json.dumps(vec)+"') AS d FROM t "+('WHERE c=1 ' if filtered else '')+'ORDER BY d LIMIT 10'
    rows=q(sql,mode)
    assert len(rows)==10 and len({i for i,d in rows})==10
    assert rows==sorted(rows,key=lambda r:(r[1],r[0]))
    for i,d in rows:
        assert i in state and (not filtered or i%3==1)
        assert math.isclose(d,math.sqrt(math.fsum((a-b)**2 for a,b in zip(state[i],vec))),rel_tol=1e-12,abs_tol=1e-12)
    if mode=='exact' or filtered or db.graphs.handle is None:assert [i for i,d in rows]==[i for d,i in truth],(mode,filtered,rows,truth)
    checks+=1
    return len({i for i,d in rows}&{i for d,i in truth})/10
for _ in range(12):
    vec=[f32(rng.uniform(-1,1)) for _ in range(32)]
    recalls.append(check(vec,'ann'));check(vec);check(vec,'ann',True)
assert sum(recalls)/len(recalls)>=.95 and db.graphs.hits>0
hits=db.graphs.hits
old=state[-1024]
q('BEGIN');q("UPDATE t SET v='"+json.dumps([0.]*32)+"' WHERE id=-1024")
state[-1024]=[0.]*32
assert db.graphs.handle is None
check([0.]*32,'ann')
q('ROLLBACK');state[-1024]=old;check([0.]*32,'ann')
q('CREATE VECTOR INDEX ix2 ON t(v)');assert db.graphs.handle
q('DELETE FROM t WHERE id=-1024');state.pop(-1024)
assert db.graphs.handle is None;check([0.]*32,'ann')
q('CREATE VECTOR INDEX ix3 ON t(v)');assert db.graphs.handle
q('BEGIN');q('DELETE FROM t WHERE id=-1023');saved=state.pop(-1023)
check([0.]*32,'ann');q('ROLLBACK');state[-1023]=saved
check([0.]*32,'ann')
db.close();db=Database('/data');assert db.graphs.handle
check([0.]*32,'ann');check([0.]*32)
db.close()
print(json.dumps({'status':'PASS','oracle_queries':checks,'ann_mean_recall':sum(recalls)/len(recalls),'graph_hits_before_mutation':hits,'rows':2048,'dimensions':32,'coverage':['negative IDs','graph actually used','exact unchanged','filtered exact','transaction update visibility','rollback','committed delete invalidation','rebuild with excluded rows','restart rebuild']}))
'''
if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--image',default='minivectordb:graph-candidate')
    parser.add_argument('--output',default='protocol-results/graph-boundaries')
    args=parser.parse_args()
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=16m','--env','MINIDB_ANN_GRAPH=1','--entrypoint','python3',args.image,'-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/args.output;out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
