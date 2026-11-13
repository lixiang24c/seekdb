"""Workloads and an independent, bounded-error selection oracle (outside timing)."""
import hashlib
import json
import math
import random
import struct
import time
from array import array
from pathlib import Path


def dataset(out, rows, dim, queries, distribution, seed):
    rng = random.Random(seed)
    def vector():
        return array('f', (rng.randrange(-64, 65) / 8 if distribution == 'grid' else rng.uniform(-8, 8)
                           for _ in range(dim)))
    vectors = [vector() for _ in range(rows)]
    query_vectors = [vector() for _ in range(queries)]
    inserts = ['INSERT INTO t VALUES ' + ','.join(
        f"({i},{i%10},'{('x'*512)}','{json.dumps(list(vectors[i]))}')"
        for i in range(start, min(rows, start + 100))) for start in range(0, rows, 100)]
    statements = ["SELECT id,L2_DISTANCE(v,'" + json.dumps(list(q)) + "') AS d FROM t "
                  + (f'WHERE c={(i//2)%10} ' if i % 2 else '') + 'ORDER BY d LIMIT 10'
                  for i, q in enumerate(query_vectors)]
    digest = hashlib.sha256()
    for v in vectors + query_vectors: digest.update(struct.pack('<' + str(dim) + 'f', *v))
    out.mkdir(parents=True, exist_ok=True)
    (out / 'dataset.json').write_text(json.dumps({'rows': rows, 'dim': dim, 'queries': queries,
        'distribution': distribution, 'seed': seed, 'float32_sha256': digest.hexdigest()}, indent=2))
    # Keep the full exact SQL workload for every failure/reproduction.
    with (out / 'workload.sql').open('w') as stream:
        for sql in inserts + statements: stream.write(sql + '\n')
    try:
        import numpy as np
    except ImportError:
        np = None
    matrix = np.asarray(vectors, dtype=np.float64) if np is not None else None
    truth = []
    for n, q in enumerate(query_vectors):
        ids = list(range((n//2)%10, rows, 10)) if n % 2 else list(range(rows))
        if np is None:
            candidates = ids
        else:
            # Use direct binary64 subtraction and multiplication, never norm/dot
            # cancellation. A deliberately loose positive-sum error interval
            # contains fsum of these same terms. Recompute every possible winner.
            distances = np.empty(len(ids))
            for start in range(0, len(ids), 4096):
                diff = matrix[ids[start:start+4096]] - np.asarray(q, dtype=np.float64)
                distances[start:start+4096] = np.sum(diff * diff, axis=1)
            error = 8 * dim * 2.220446049250313e-16
            lower = distances / (1 + error)
            upper = distances / (1 - error)
            cutoff = float(np.partition(upper, 9)[9])
            candidates = [ids[j] for j in np.flatnonzero(lower <= cutoff)]
        ranked = sorted((math.fsum((a-b)*(a-b) for a,b in zip(vectors[i], q)), i)
                        for i in candidates)[:10]
        if np is not None:
            omitted = lower[lower > cutoff]
            assert not len(omitted) or float(omitted.min()) > ranked[-1][0]
        truth.append([[i, math.sqrt(distance)] for distance, i in ranked])
    (out / 'oracle.json').write_text(json.dumps(truth))
    return vectors, inserts, statements, truth


def check_answers(actual, expected):
    assert len(actual) == len(expected)
    for i, (rows, truth) in enumerate(zip(actual, expected)):
        assert [r[0] for r in rows] == [r[0] for r in truth], (i, rows, truth)
        assert [struct.pack('<d', r[1]) for r in rows] == [struct.pack('<d', r[1]) for r in truth], (i, rows, truth)


def request(session, sql):
    result = session.request(sql, mode='exact')
    assert result['ok'], result
    return result


def workload(session, observer, spec, inserts, statements, order, sample):
    sample.update(seconds={}, resources={}, latencies_seconds={})
    def snap(name, drain=False):
        sample['resources'][name] = observer.snapshot(drain)
    def phase(name, action):
        sample['active_phase'] = name
        start = time.perf_counter(); result = action()
        sample['seconds'][name] = time.perf_counter() - start
        return result
    snap('after_startup')
    phase('schema', lambda: request(session, f"CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR({spec['dim']}))"))
    snap('after_schema')
    def load():
        for sql in inserts:
            request(session, 'BEGIN')
            assert request(session, sql)['affected_rows'] == 100
            request(session, 'COMMIT')
    phase('write', load); snap('after_write', True)
    phase('index', lambda: request(session, 'CREATE VECTOR INDEX ix ON t(v)')); snap('after_index', True)
    first = phase('first_queries', lambda: [request(session, sql)['rows'] for sql in statements[:20]])
    snap('after_first_queries')
    # Retain first-touch timings; this is not a certified globally cold cache.
    warmed = phase('warmup', lambda: [request(session, sql)['rows'] for sql in statements[:20]])
    snap('after_warmup')
    answers = [None] * len(statements)
    for group in order:
        sample['active_phase'] = group
        latencies = sample['latencies_seconds'][group] = []
        start = time.perf_counter()
        for i in range(int(group == 'filtered'), len(statements), 2):
            one = time.perf_counter()
            answers[i] = request(session, statements[i])['rows']
            latencies.append(time.perf_counter() - one)
        sample['seconds'][group] = time.perf_counter() - start
        snap('after_' + group, True)
    sample['seconds']['query'] = sample['seconds']['filtered'] + sample['seconds']['unfiltered']
    snap('final_writeback_settled', True)
    return answers, first + warmed


def persistence(session, vectors):
    session.shutdown(); session.start()
    for start in range(0, len(vectors), 100):
        rows = request(session, f'SELECT id,c,body,v FROM t WHERE id>={start} AND id<{start+100} ORDER BY id')['rows']
        assert rows == [[i, i % 10, 'x' * 512, list(vectors[i])] for i in range(start, min(start+100, len(vectors)))]
    # Exercise mutations against the same large on-disk index, after measuring.
    expression = "L2_DISTANCE(v,'" + json.dumps(list(vectors[0])) + "')"
    query = 'SELECT id,' + expression + ' AS d FROM t ORDER BY d LIMIT 10'
    before = request(session, query)['rows']
    request(session, 'BEGIN'); request(session, 'DELETE FROM t WHERE id=0')
    duplicate = session.request("INSERT INTO t VALUES (1,0,'dup','" + json.dumps(list(vectors[0])) + "')")
    assert not duplicate['ok']
    request(session, "INSERT INTO t VALUES (0,0,'" + 'x'*512 + "','" + json.dumps(list(vectors[0])) + "')")
    assert request(session, query)['rows'] == before
    request(session, 'ROLLBACK')
    assert request(session, query)['rows'] == before
    request(session, 'BEGIN'); request(session, 'UPDATE t SET c=9 WHERE id=0'); request(session, 'COMMIT')
    session.shutdown(); session.start()
    assert request(session, 'SELECT id,c FROM t WHERE id=0')['rows'] == [[0,9]]
    session.shutdown()
