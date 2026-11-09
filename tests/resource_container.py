"""Run inside a dedicated constrained container; writes only to /data."""
import json
from pathlib import Path
from minivectordb.core import Database
from minivectordb.sql import SQL
root=Path('/data');db=Database(root);sql=SQL(db)
sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, payload VARCHAR(1024))')
for first in range(0,80000,100):
 values=','.join("(%d,'%s')"%(i,'x'*1024) for i in range(first,first+100))
 sql.execute('INSERT INTO t VALUES '+values)
db.close();db=Database(root)
assert sum(1 for _ in db.rows('t'))==80000
data_bytes=sum(p.stat().st_size for p in root.iterdir() if p.is_file())
from tests.resource_metrics import snapshot
stats=snapshot()
assert data_bytes>stats['limit']
print(json.dumps({'result':'PASS_CONTROLLER','rows':80000,'data_bytes':data_bytes,'memory':stats,
                  'scope':'local cgroup-controller test; raw peak and strict-peak status retained, not official configuration'}))
