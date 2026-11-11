#!/usr/bin/env python3
"""Reuse SQL projection oracle with immutable-map invalidation assertions."""
import json,subprocess
from pathlib import Path
from projection_distances import PROBE

PROBE=PROBE.replace("db=Database('/data');checks=0", "db=Database('/data');checks=0;events={'queries_with_cached_maps':0,'write_invalidations':0}")
PROBE=PROBE.replace("    r=db.execute(sql)", """    r=db.execute(sql)
    assert db.index_reader.bytes<=8<<20 and len(db.index_reader.maps)<=16
    if hasattr(r,'columns'):
        if db.index_reader.maps:events['queries_with_cached_maps']+=1
    elif sql.split()[0] in ('CREATE','INSERT','UPDATE','DELETE','ROLLBACK'):
        assert not db.index_reader.maps
        events['write_invalidations']+=1""")
PROBE=PROBE.replace("{'status':'PASS','oracle_queries':checks", "{'status':'PASS','mapping_events':events,'oracle_queries':checks")
PROBE=PROBE.replace('before=copy.deepcopy(state)', '''from minidb.sql import DBError
nodes=tuple(db.index_reader.maps.values())
try:q("INSERT INTO t VALUES(9000,0,'new','"+json.dumps([0.]*128)+"'),("+str(ids[0])+",0,'dup','"+json.dumps([0.]*128)+"')")
except DBError as e:assert e.code=='duplicate_key'
else:assert False
assert tuple(db.index_reader.maps.values())==nodes
check()
q('BEGIN');check();q('COMMIT');assert not db.index_reader.maps;check()
before=copy.deepcopy(state)''')
r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=24m','--entrypoint','python3','minivectordb:mmap-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
assert r.returncode==0,(r.stdout,r.stderr)
result=json.loads(r.stdout)
assert result['mapping_events']['queries_with_cached_maps']>0 and result['mapping_events']['write_invalidations']>0
out=Path(__file__).resolve().parents[1]/'protocol-results/mapped-views';out.mkdir(parents=True,exist_ok=True)
(out/'result.json').write_text(r.stdout);print(r.stdout,end='')
