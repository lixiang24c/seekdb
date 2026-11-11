#!/usr/bin/env python3
"""Visible-version oracle across bounded overlay compaction and index maintenance."""
import json
from pathlib import Path
import sys
import tempfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from minidb.engine import Database
from minidb.sql import DBError
with tempfile.TemporaryDirectory() as directory:
    db=Database(directory)
    def q(sql):
        r=db.execute(sql)
        if not hasattr(r,'columns'):return r
        if r.path is None:return [json.loads(x) for x in r.encoded_rows]
        try:return [json.loads(x) for x in r.path.read_text().splitlines()]
        finally:r.path.unlink()
    q('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(2))')
    q('INSERT INTO t VALUES '+','.join(f"({i},0,'[{i},0]')" for i in range(500)))
    q('CREATE VECTOR INDEX ix ON t(v)')
    db.close()
    manifest_path=Path(directory)/'manifest.json'
    legacy=json.loads(manifest_path.read_text());legacy['format']=1;manifest_path.write_text(json.dumps(legacy))
    db=Database(directory)
    state={i:[0,[float(i),0.0]] for i in range(500)}
    files={p['file'] for p in db.store.segments(db.store.tables['t'])}
    q("UPDATE t SET v='[-1,0]',c=1 WHERE id=4");state[4]=[1,[-1.0,0.0]]
    assert json.loads(manifest_path.read_text())['format']==2
    assert files.issubset({p['file'] for p in db.store.segments(db.store.tables['t'])})
    def check():
        assert q('SELECT * FROM t ORDER BY id')==[[i,*state[i]] for i in sorted(state)]
        assert q('SELECT id FROM t ORDER BY id DESC LIMIT 7')==[[i] for i in sorted(state,reverse=True)[:7]]
        assert q("SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10")==[[i] for i in sorted(state,key=lambda i:(sum(x*x for x in state[i][1]),i))[:10]]
        assert q("SELECT id FROM t WHERE c=1 ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10")==[[i] for i in sorted((i for i in state if state[i][0]==1),key=lambda i:(sum(x*x for x in state[i][1]),i))[:10]]
    check()
    for i in range(100):
        q(f"UPDATE t SET v='[{i%3},0]',c=1 WHERE id={i}");state[i]=[1,[float(i%3),0.0]]
        if i%17==0:check()
    # Deletes cross the exclusion threshold with no replacement segments.
    for i in range(120,210):q(f'DELETE FROM t WHERE id={i}');state.pop(i)
    check()
    q('BEGIN');q("UPDATE t SET v='[99,99]' WHERE id=4");q('DELETE FROM t WHERE id=5');q('ROLLBACK');check()
    q('UPDATE t SET c=2 WHERE id>=30 AND id<40')
    for i in range(30,40):state[i][0]=2
    q('DELETE FROM t WHERE id>=40 AND id<45')
    for i in range(40,45):state.pop(i)
    q("INSERT INTO t VALUES(40,1,'[0,0]'),(120,1,'[0,0]')")
    state[40]=[1,[0.0,0.0]];state[120]=[1,[0.0,0.0]]
    try:q("INSERT INTO t VALUES(40,1,'[0,0]')")
    except DBError as e:assert e.code=='duplicate_key'
    else:raise AssertionError('live overlay duplicate accepted')
    q('CREATE VECTOR INDEX ix2 ON t(v)');check()
    assert len(db.store.segments(db.store.tables['t']))<=32
    db.close();db=Database(directory);check();db.close()
print(json.dumps({'status':'PASS','updates':101,'point_deletes':90,'checks':['base retained','overlay compaction','delete threshold compaction','ordinary/range/reverse/filtered/unfiltered oracle','rollback','general mutation','deleted key reinsertion','live duplicate rejection','second index','restart']}))
