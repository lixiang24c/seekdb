#!/usr/bin/env python3
"""Query-local distance reuse, projection dependencies and sort fallback."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import copy,json,math,os
from minidb import engine
from minidb.engine import Database
from minidb.storage import Codec
db=Database('/data');checks=0
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
ids=[-(1<<63),(1<<63)-1]+list(range(-159,159))
state={i:(j%3,[float(j%17)/8]*128) for j,i in enumerate(ids)}
q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(128))')
for start in range(0,len(ids),100):
    q('INSERT INTO t VALUES '+','.join(f"({i},{state[i][0]},'{('x'*512)}','{json.dumps(state[i][1])}')" for i in ids[start:start+100]))
literal=json.dumps([.25]*128);other=json.dumps([.5]*128)
distance="L2_DISTANCE(v,'"+literal+"')"
distance2="L2_DISTANCE(v,'"+other+"')"
def truth(filtered=False,k=10,reverse=False):
    ranked=[(math.fsum((v-.25)**2 for v in vec),i) for i,(c,vec) in state.items() if not filtered or c==1]
    return sorted(ranked,key=lambda p:((-p[0] if reverse else p[0]),p[1]))[:k]
def check(filtered=False,k=10,reverse=False):
    global checks
    where='WHERE c=1 ' if filtered else ''
    rows=q('SELECT id,'+distance+' AS d FROM t '+where+'ORDER BY d'+(' DESC' if reverse else '')+' LIMIT '+str(k))
    assert rows==[[i,math.sqrt(d)] for d,i in truth(filtered,k,reverse)]
    checks+=1
check();check(True)
q('CREATE VECTOR INDEX ix ON t(v)')
calls=[];decodes=[]
original=engine.squared;original_decode=Codec.decode_fields
def squared(a,b):calls.append(1);return original(a,b)
def decode(self,raw,fields):decodes.append(set(fields));return original_decode(self,raw,fields)
engine.squared=squared;Codec.decode_fields=decode
check();assert not calls and decodes and all(3 not in fields for fields in decodes)
rows=q('SELECT id,'+distance+' AS d,'+distance+' AS again FROM t ORDER BY d LIMIT 10')
assert rows==[[i,math.sqrt(d),math.sqrt(d)] for d,i in truth()]
assert not calls;checks+=1
calls.clear()
rows=q('SELECT id,'+distance+' AS d,'+distance2+' AS other FROM t ORDER BY d LIMIT 10')
assert rows==[[i,math.sqrt(d),math.sqrt(math.fsum((v-.5)**2 for v in state[i][1]))] for d,i in truth()]
assert len(calls)==10;checks+=1
decodes.clear();calls.clear()
rows=q('SELECT id,v,'+distance+' AS d FROM t ORDER BY d LIMIT 10')
assert rows==[[i,state[i][1],math.sqrt(d)] for d,i in truth()]
assert any(3 in fields for fields in decodes) and not calls;checks+=1
engine.squared=original;Codec.decode_fields=original_decode
# Explicit expression ordering, native-off path, descending and range plans.
assert q('SELECT id,'+distance+' AS d FROM t ORDER BY '+distance+' LIMIT 10')==[[i,math.sqrt(d)] for d,i in truth()];checks+=1
check(True);check(True)
os.environ['MINIDB_NATIVE']='0';check();check(True);os.environ.pop('MINIDB_NATIVE')
check(reverse=True);check(k=0)
ranked=sorted((math.fsum((v-.25)**2 for v in vec),i) for i,(c,vec) in state.items() if i>=0)[:10]
assert q('SELECT id,'+distance+' AS d FROM t WHERE id>=0 ORDER BY d LIMIT 10')==[[i,math.sqrt(d)] for d,i in ranked];checks+=1
ranked=sorted((math.fsum((v-.25)**2 for v in vec),c,i) for i,(c,vec) in state.items())[:10]
assert q('SELECT id,'+distance+' AS d FROM t ORDER BY d,c LIMIT 10')==[[i,math.sqrt(d)] for d,c,i in ranked];checks+=1
fallbacks=[];original_topk=db.vector_topk
def tracked(*a,**kw):
    result=original_topk(*a,**kw);fallbacks.append(result is None);return result
db.vector_topk=tracked
check(k=1024);assert fallbacks==[True]
db.vector_topk=original_topk
before=copy.deepcopy(state)
q('BEGIN');q("UPDATE t SET v='"+json.dumps([.25]*128)+f"' WHERE id={ids[0]}");state[ids[0]]=(state[ids[0]][0],[.25]*128)
check();q('DELETE FROM t WHERE id=-159');state.pop(-159);check(True)
q('ROLLBACK');state=before;check()
db.close();db=Database('/data');check();db.close()
print(json.dumps({'status':'PASS','oracle_queries':checks,'rows':320,'dimensions':128,'coverage':['raw scan before index','no duplicate Python distance computation','vector decoding omitted when unused','same/different query vectors','explicit vector projection','alias/expression ordering','filter cache reuse','native off','DESC/range/multi-key plans','LIMIT0','large-row heap fallback to sort','transaction mutation/delete','rollback','extreme primary keys','restart']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=24m','--entrypoint','python3','minivectordb:projection-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/projection-distances';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
