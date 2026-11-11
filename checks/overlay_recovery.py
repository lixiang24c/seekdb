#!/usr/bin/env python3
"""Kill immediately before/after publishing a point change that triggers compaction."""
import json
from pathlib import Path
import tempfile
from recovery import Client
results=[]
for cut in ('before','after'):
    with tempfile.TemporaryDirectory() as directory:
        data=Path(directory);c=Client(data,cut)
        c.query('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(1))')
        c.query('INSERT INTO t VALUES '+','.join(f"({i},0,'[{i}]')" for i in range(100)))
        c.query('CREATE VECTOR INDEX ix ON t(v)')
        for i in range(31):c.query(f"UPDATE t SET c=1,v='[0]' WHERE id={i}")
        manifest=json.loads((data/'manifest.json').read_text())
        assert len(manifest['tables']['t']['segments'])==32
        (data/'armed').touch()
        c.p.stdin.write(json.dumps({'id':1,'op':'execute','sql':"UPDATE t SET c=1,v='[0]' WHERE id=31"})+'\n');c.p.stdin.flush()
        c.p.wait(timeout=10);assert c.p.returncode==86
        c.p.stdin.close();c.p.stdout.close();c.p.stderr.close()
        for _ in range(2):
            r=Client(data)
            assert r.query('SELECT c FROM t WHERE id=31')['rows']==[[int(cut=='after')]]
            assert r.query("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0]') LIMIT 32")['rows']==[[i] for i in range(32)]
            r.close()
        results.append({'cut':cut,'compaction':'PASS','two_restarts':'PASS'})
print(json.dumps({'status':'PASS','scenarios':results}))
