#!/usr/bin/env python3
"""Predicate cache versioning, memory fallback and metadata-only mutations."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from minidb.engine import Database
with tempfile.TemporaryDirectory() as directory:
    db=Database(directory)
    def q(sql):
        r=db.execute(sql)
        if not hasattr(r,'columns'):return r
        if r.path is None:return [json.loads(x) for x in r.encoded_rows]
        try:return [json.loads(x) for x in r.path.read_text().splitlines()]
        finally:r.path.unlink()
    q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(1))')
    assert db.store.tables['t']['segments']==[]
    q('INSERT INTO t VALUES '+','.join(f"({i},0,'[{i}]')" for i in range(5000)))
    q('CREATE VECTOR INDEX ix ON t(v)')
    def near(cond):return q(f"SELECT id FROM t WHERE {cond} ORDER BY L2_DISTANCE(v,'[0]') LIMIT 3")
    assert near('c=0')==[[0],[1],[2]]
    assert any(ids is None for ids,cost in db.filter_cache.values())
    assert near('c=0')==[[0],[1],[2]]
    assert near('c=1')==[]
    q('BEGIN');q('UPDATE t SET c=1 WHERE id=4')
    assert near('c=1')==[[4]]
    q('ROLLBACK');assert near('c=1')==[]
    files=[p['file'] for p in db.store.segments(db.store.tables['t'])]
    assert q('UPDATE t SET c=0 WHERE c=0')['affected_rows']==5000
    assert files==[p['file'] for p in db.store.segments(db.store.tables['t'])]
    q('BEGIN');assert q('DELETE FROM t')['affected_rows']==5000
    assert db.store.tables['t']['segments']==[] and near('c=0')==[]
    q('ROLLBACK');assert near('c=0')==[[0],[1],[2]]
    assert q('DELETE FROM t')['affected_rows']==5000
    q("INSERT INTO t VALUES(9,1,'[0]')")
    assert near('c=1')==[[9]]
    assert db.filter_cache_bytes<=512*1024
    db.close();db=Database(directory);assert near('c=1')==[[9]];db.close()
print(json.dumps({'status':'PASS','checks':['empty create','cache oversized fallback','cached empty membership invalidated by transaction','rollback reuses committed membership','whole-set no-op retains files','delete all rollback/commit/reinsert/restart','512KiB cache budget']}))
# Force several external merge passes while excluding a wide unused column.
import minidb.engine as engine
with tempfile.TemporaryDirectory() as directory:
    db=Database(directory)
    q('CREATE TABLE t(body VARCHAR(10000),id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(1))')
    q('INSERT INTO t VALUES '+','.join(f"('{'x'*8000}',{i},{i%7},'[{i%11}]')" for i in range(200)))
    previous_budget,previous_fanin=engine.SORT_BYTES,engine.MERGE_FANIN
    engine.SORT_BYTES,engine.MERGE_FANIN=1024,4
    try:
        assert q('SELECT id FROM t ORDER BY c DESC,id DESC')==[[i] for i in sorted(range(200),key=lambda i:(-(i%7),-i))]
        assert q("SELECT id,L2_DISTANCE(v,'[0]') AS d FROM t ORDER BY d") == [[i,float(i%11)] for i in sorted(range(200),key=lambda i:(i%11,i))]
        assert not list(Path(directory).glob('mv-*.sort'))
    finally:
        engine.SORT_BYTES,engine.MERGE_FANIN=previous_budget,previous_fanin
        db.close()
print(json.dumps({'projected_external_sort':'PASS','forced_chunk_bytes':1024,'merge_fanin':4,'wide_unused_column_bytes':8000,'rows':200}))
