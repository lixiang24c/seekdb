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
peak=int(Path('/sys/fs/cgroup/memory.peak').read_text())
limit=int(Path('/sys/fs/cgroup/memory.max').read_text())
assert data_bytes>limit
assert peak<=limit
print(json.dumps({'result':'PASS','rows':80000,'data_bytes':data_bytes,'cgroup_memory_peak':peak,'cgroup_memory_limit':limit,'scope':'local container test, not official resource configuration'}))
