"""Large persisted vector index under a smaller hard cgroup memory limit."""
import json,math
from pathlib import Path
from minivectordb.core import Database
from minivectordb.sql import SQL

root=Path('/data');db=Database(root)
db.create('t',{'id':['BIGINT'],'tag':['BIGINT'],'v':['VECTOR',128]},'id')
rows=140000
for first in range(0,rows,512):
    db.insert('t',({'id':i,'tag':i%7,'v':[float(i)]*128} for i in range(first,min(first+512,rows))))
db.create_vector_index('ix','t','v');db.close();db=Database(root);sql=SQL(db)
zero='['+','.join(['0']*128)+']'
for clause,expected in [('',list(range(10))),(' WHERE tag=3',[3+7*i for i in range(10)])]:
    result=list(sql.execute(f'SELECT id,L2_DISTANCE(v,{zero}) AS d FROM t{clause} ORDER BY d LIMIT 10')['rows'])
    assert [r[0] for r in result]==expected
    assert all(math.isclose(distance,key*math.sqrt(128),rel_tol=1e-12,abs_tol=1e-12) for key,distance in result)
assert db._scan_map_bytes==0  # Index exceeds retention budget; only demand-paged scan is used.
assert db._query_mask_bytes<=db.QUERY_MASK_LIMIT
assert db._filter_cache_bytes<=db.FILTER_CACHE_LIMIT
size=sum(p.stat().st_size for p in root.iterdir() if p.is_file())
from tests.resource_metrics import snapshot
stats=snapshot()
assert size>stats['limit']
print(json.dumps({'result':'PASS_CONTROLLER','rows':rows,'dimension':128,'data_bytes':size,'memory':stats,
                  'retained_map_bytes':db._scan_map_bytes,'mask_cache_bytes':db._query_mask_bytes,
                  'filter_cache_bytes':db._filter_cache_bytes,'scope':'local cgroup large-index verification; raw peak retained, not official configuration'}))
db.close()
