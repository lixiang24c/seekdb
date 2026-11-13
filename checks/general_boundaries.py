#!/usr/bin/env python3
"""Focused checks for offset index compatibility and result spill boundaries."""
import json
from pathlib import Path
import struct
import sys
import tempfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from minidb.engine import Database, QueryResult
from minidb.sql import DBError

with tempfile.TemporaryDirectory() as directory:
    db = Database(directory)
    def q(sql):
        result = db.execute(sql)
        if isinstance(result, QueryResult):
            if result.path is None: return [json.loads(row) for row in result.encoded_rows]
            try: return [json.loads(row) for row in result.path.read_text().splitlines()]
            finally: result.path.unlink()
        return result
    q('CREATE TABLE t(body VARCHAR(100000),id BIGINT PRIMARY KEY,v VECTOR(2))')
    q("INSERT INTO t VALUES ('a',-9,'[1,0]'),('b',2,'[0,1]')")
    original = [p['file'] for p in db.store.segments(db.store.tables['t'])]
    q('BEGIN'); q('CREATE VECTOR INDEX ix ON t(v)')
    assert original == [p['file'] for p in db.store.segments(db.store.tables['t'])]
    q('ROLLBACK'); assert db.store.tables['t']['indexes'] == []
    q('CREATE VECTOR INDEX ix ON t(v)'); q('CREATE VECTOR INDEX ix2 ON t(v)')
    assert original == [p['file'] for p in db.store.segments(db.store.tables['t'])]
    assert q("SELECT id FROM t WHERE id=-9 AND id=2") == []
    assert q("UPDATE t SET body='z' WHERE id=-9 AND body='b'")['affected_rows'] == 0
    assert q("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE id=-9 ORDER BY d LIMIT 1") == [[-9,1.0]]
    q("INSERT INTO t VALUES ('" + '界'*30000 + "',3,'[2,0]')")
    assert q('SELECT body FROM t WHERE id=3') == [['界'*30000]]
    # Emulate a database produced by the preceding version, without rewriting rows.
    legacy_rows = {part['file']: list(db.store.part_records(db.store.tables['t'], part))
                   for part in db.store.segments(db.store.tables['t'])}
    db.close()
    manifest = json.loads((Path(directory)/'manifest.json').read_text())
    for part in manifest['tables']['t']['segments']:
        # Materialize the actual legacy full-row format, including a separate
        # key-only directory. Never overwrite a columnar file with key bytes.
        records = legacy_rows[part['file']]
        p = Path(directory)/part['file']
        p.write_bytes(b'MVROW001'+b''.join(struct.pack('<I',len(raw))+raw for _,raw in records))
        part['keys'] = part['file']+'.legacy-keys'
        (Path(directory)/part['keys']).write_bytes(b'MVKEY001'+b''.join(struct.pack('<q',key) for key,_ in records))
        part.pop('columnar',None);part.pop('key_offset',None)
    (Path(directory)/'manifest.json').write_text(json.dumps(manifest))
    db = Database(directory)
    assert q('SELECT id FROM t WHERE id=-9') == [[-9]]
    assert q("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 2") == [[-9,1.0],[2,1.0]]
    try: q("INSERT INTO t VALUES ('dup',-9,'[0,0]')")
    except DBError as exc: assert exc.code == 'duplicate_key'
    else: raise AssertionError('duplicate accepted')
    q("UPDATE t SET v='[0,0]' WHERE id=-9")
    q('DELETE FROM t WHERE id=2')
    q("INSERT INTO t VALUES ('new',2,'[0,0]')")
    db.close(); db = Database(directory)
    assert q("SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t ORDER BY d LIMIT 2") == [[-9,0.0],[2,0.0]]
    db.close()
print(json.dumps({'status':'PASS','checks':['index retains row files','index rollback','conflicting PK predicates','point predicate rejection','point distance order','Unicode result spill','old keys lookup/TopK/duplicate rejection','mixed old/new index update/delete/reinsert/restart']}))
