#!/usr/bin/env python3
"""64MiB container with a real vector index exceeding that memory limit."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import json,time
from minidb.engine import Database
started=time.monotonic();db=Database('/data');n=70000;dim=256
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(256))')
q('CREATE VECTOR INDEX ix ON t(v)')
vec=[0.]*dim
# The real streaming storage API prepares the fixture, avoiding SQL generation
# overhead. All tested reads and mutations below use the SQL executor.
table=db.store.rewrite(db.store.tables['t'],([i,i%100,vec] for i in range(n)))
db.install('t',table)
path=db.store.index_paths(table)[0];size=path.stat().st_size
assert size>64<<20
sql="SELECT id,L2_DISTANCE(v,'"+json.dumps(vec)+"') AS d FROM t ORDER BY d LIMIT 10"
expected=[[i,0.] for i in range(10)]
assert q(sql)==expected and not db.index_reader.maps
filtered="SELECT id FROM t WHERE c=5 ORDER BY L2_DISTANCE(v,'"+json.dumps(vec)+"') LIMIT 10"
for _ in range(3):assert q(filtered)==[[5+100*i] for i in range(10)]
assert not db.index_reader.maps
q('BEGIN');q("UPDATE t SET v='"+json.dumps([1.]*dim)+"' WHERE id=0")
assert q(sql)==[[i,0.] for i in range(1,11)]
assert db.index_reader.maps  # Only the small replacement index can be mapped.
q('ROLLBACK');assert not db.index_reader.maps
assert q(sql)==expected;db.close()
db=Database('/data');assert q(sql)==expected;db.close()
print(json.dumps({'status':'PASS','rows':n,'dimensions':dim,'index_bytes':size,'oracle_queries':7,'elapsed_seconds':time.monotonic()-started,'scope':'Real storage API fixture;SQL queries/update/rollback/restart in64MiB;large index stays on buffered path','coverage':['index larger than memory','bounded exact TopK','selective filter cache','large-index buffered fallback','small overlay mapping','rollback invalidation','restart']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--mount','type=volume,destination=/data','--entrypoint','python3','minivectordb:mmap-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=90)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/large-vector-scan';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
