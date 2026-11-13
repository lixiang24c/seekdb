#!/usr/bin/env python3
# Also executable directly from checks/ on the host.
import sys
from pathlib import Path
if '__file__' in globals():
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
"""Budget failures, mapping transitions, transactions and independent exact oracle."""
import hashlib
import json
import math
import random
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch
from minidb import indexio
from minidb.engine import Database
from minidb.sql import DBError

checks = 0
real_path = Path
with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    cg = root / 'sys/fs/cgroup'
    cg.mkdir(parents=True)
    (root / 'proc/self').mkdir(parents=True)
    read_text, exists = Path.read_text, Path.exists
    def fake_read(path, *args, **kwargs):
        return read_text(root / str(path).lstrip('/'), *args, **kwargs)
    def fake_exists(path):
        return exists(root / str(path).lstrip('/'))
    def group(path, maximum='16777216', high='max', stats='anon 4194304\nkernel 1048576\nfile 9999999\n'):
        path.mkdir(parents=True, exist_ok=True)
        for name, text in (('memory.max', maximum), ('memory.high', high), ('memory.stat', stats)):
            (path / name).write_text(text)
    def budget(expected):
        global checks
        with patch.object(Path, 'read_text', fake_read), patch.object(Path, 'exists', fake_exists):
            assert indexio.file_cache_budget() == expected, expected
        checks += 1
    (root / 'proc/self/cgroup').write_text('0::/\n')
    group(cg)
    budget(10 << 20)
    group(cg, high=str(12 << 20)); budget(6 << 20)
    group(cg, maximum='0'); budget(0)
    group(cg, maximum='max', high='max'); budget(None)
    group(cg, maximum='max', high=str(12 << 20)); budget(6 << 20)
    for value in ('-1', 'broken', ''):
        group(cg, maximum=value); budget(None)
    for stats in ('anon 1\n', 'anon -1\nkernel 1\n', 'anon x\nkernel 1\n', 'invalid\n'):
        group(cg, stats=stats); budget(None)
    group(cg); (cg / 'memory.high').unlink(); budget(None)
    (root / 'proc/self/cgroup').write_text('5:memory:/legacy\n'); budget(None)
    (root / 'proc/self/cgroup').write_text('0::/../../other\n'); budget(None)
    (root / 'proc/self/cgroup').write_text('0::/child\n')
    group(cg); group(cg / 'child', maximum=str(64 << 20)); budget(10 << 20)
    group(cg, maximum=str(12 << 20)); budget(6 << 20)
    group(cg / 'child', maximum='max'); budget(6 << 20)
    (cg / 'memory.max').unlink(); budget(None)
    group(cg / 'child'); budget(10 << 20)
    with patch.object(Path, 'read_text', side_effect=PermissionError):
        assert indexio.file_cache_budget() is None
    checks += 1

    reader = indexio.IndexReader()
    magic = b'01234567'
    payload = bytes(range(256)) * 4096
    path = root / 'index'
    path.write_bytes(magic + payload)
    segments = [(path, set())]
    table = {}
    with patch.object(indexio, 'file_cache_budget', return_value=64 << 20) as estimate:
        big = reader.block_size(table, segments, 520)
        assert not reader.streaming and big <= 4 << 20 and big % 520 == 0
        assert reader.block_size(table, segments, 520) == big and estimate.call_count == 1
    with reader.blocks(path, big, magic, mapped=True) as chunks:
        assert b''.join(bytes(data) for data, pointer in chunks) == payload
    assert reader.maps
    old = next(iter(reader.maps.values()))
    for value in (0, None):
        with patch.object(indexio, 'file_cache_budget', return_value=value):
            small = reader.block_size({}, segments, 520)
        assert reader.streaming and small <= 256 << 10 and small % 520 == 0
        with reader.blocks(path, small, magic, mapped=True) as chunks:
            parts = list(chunks)
            assert all(isinstance(data, bytes) and pointer is None and len(data) <= small for data, pointer in parts)
            assert b''.join(data for data, pointer in parts) == payload
        assert not reader.maps and reader.bytes == 0
        checks += 1
    assert old.mapping.closed
    path.write_bytes(magic + payload[:10000])
    with reader.blocks(path, small, magic, mapped=True) as chunks:
        assert b''.join(bytes(data) for data, pointer in chunks) == payload[:10000]
    assert reader.maps
    reader.clear()
    assert not reader.maps and reader.profile_table is None and not reader.streaming
    path.write_bytes(b'invalid!' + payload)
    try:
        with reader.blocks(path, small, magic, mapped=True) as chunks: list(chunks)
    except OSError: pass
    else: raise AssertionError('header accepted')
    checks += 4

    rng = random.Random(49731)
    f32 = lambda x: struct.unpack('<f', struct.pack('<f', x))[0]
    state = {i: (i % 7, [f32(rng.uniform(-4, 4)) for _ in range(32)]) for i in range(2500)}
    database_path = root / 'db'
    db = Database(database_path)
    def execute(sql):
        result = db.execute(sql)
        if not hasattr(result, 'columns'): return result
        if result.path is None: return [json.loads(row) for row in result.encoded_rows]
        try: return [json.loads(row) for row in result.path.read_text().splitlines()]
        finally: result.path.unlink()
    def values(i):
        category, vector = state[i]
        return f"({i},{category},'{json.dumps(vector)}')"
    def verify():
        global checks
        query = [f32(rng.uniform(-3, 3)) for _ in range(32)]
        expression = "L2_DISTANCE(v,'" + json.dumps(query) + "')"
        for category in (None, 0, 1):
            where = '' if category is None else f' WHERE c={category}'
            ranked = sorted((math.fsum((a-b)*(a-b) for a,b in zip(vec, query)), i)
                            for i,(c,vec) in state.items() if category is None or c == category)[:10]
            expected = [[i, math.sqrt(distance)] for distance,i in ranked]
            actual = execute('SELECT id,' + expression + ' AS d FROM t' + where + ' ORDER BY d LIMIT 10')
            assert [r[0] for r in actual] == [r[0] for r in expected]
            assert [struct.pack('<d', r[1]) for r in actual] == [struct.pack('<d', r[1]) for r in expected]
            if category is not None:
                assert execute(f'SELECT id FROM t WHERE c={category} ORDER BY id') == [[i] for i,(c,v) in sorted(state.items()) if c == category]
            checks += 1
    execute('CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(32))')
    for start in range(0, 2500, 100): execute('INSERT INTO t VALUES ' + ','.join(values(i) for i in range(start,start+100)))
    execute('CREATE VECTOR INDEX ix ON t(v)')
    with patch.object(indexio, 'file_cache_budget', return_value=0):
        verify()
        assert db.index_reader.streaming
        assert all(node.cost <= (256 << 10) + 8 for node in db.index_reader.maps.values())
        snapshot = state.copy()
        for finish in ('ROLLBACK', 'COMMIT'):
            execute('BEGIN')
            state[3] = (6, [f32(.17)] * 32)
            execute("UPDATE t SET c=6,v='" + json.dumps(state[3][1]) + "' WHERE id=3")
            execute('DELETE FROM t WHERE id=4'); state.pop(4)
            state[4] = (2, [f32(-.21)] * 32); execute('INSERT INTO t VALUES ' + values(4))
            try: execute('INSERT INTO t VALUES ' + values(3))
            except DBError as error: assert error.code == 'duplicate_key'
            else: raise AssertionError('expected duplicate')
            verify()
            state[5] = (4, [f32(.19)] * 32)
            execute("UPDATE t SET c=4,v='" + json.dumps(state[5][1]) + "' WHERE id=5")
            verify(); execute(finish)
            if finish == 'ROLLBACK': state = snapshot.copy()
            verify()
            db.index_reader.clear(); db.store.close(); db = Database(database_path); verify()
    # Switching plans must preserve masks expressed in record offsets.
    db.index_reader.clear()
    with patch.object(indexio, 'file_cache_budget', return_value=64 << 20): verify()
    assert db.index_reader.maps
    db.index_reader.clear()
    with patch.object(indexio, 'file_cache_budget', return_value=None): verify()
    db.index_reader.clear(); db.store.close()

print(json.dumps({'status': 'PASS', 'checks': checks, 'oracle': 'independent math.fsum and float64 bytes',
                  'covers': ['nested limits', 'unavailable/malformed budget', 'mapped-buffered transitions',
                             'continuous float32', 'filter masks', 'update', 'delete/reinsert',
                             'failed statement', 'commit', 'rollback', 'restart']}))
