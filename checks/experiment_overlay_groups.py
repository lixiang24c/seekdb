import json, random, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, '.')
from minidb.engine import Database
def run(db, sql, mode=None):
    r = db.execute(sql, mode) if mode else db.execute(sql)
    if hasattr(r, 'columns'):
        if r.path is None: return [json.loads(x) for x in r.encoded_rows]
        try: return [json.loads(x) for x in r.path.read_text().splitlines()]
        finally: r.path.unlink()
    return r
rnd = random.Random(20261120)
N, D = 10000, 128
UPDATES = int(sys.argv[1]) if len(sys.argv) > 1 else 100
with tempfile.TemporaryDirectory() as d:
    db = Database(d)
    run(db, f'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR({D}))')
    for s in range(0, N, 100):
        rows = ','.join("(%d,%d,'[%s]')" % (i, i % 6, ','.join('%.4f' % rnd.random() for _ in range(D))) for i in range(s, s + 100))
        run(db, 'INSERT INTO t VALUES ' + rows)
    run(db, 'CREATE VECTOR INDEX ix ON t(v)')
    def nseg(): return len(db.store.segments(db.store.tables['t']))
    print('segments after load', nseg())
    # Experiment A
    A = []
    for k in range(UPDATES):
        pk = (k * 97) % N
        t = time.perf_counter()
        run(db, "UPDATE t SET c=%d WHERE id=%d" % (1000 + k, pk))
        A.append((k + 1, round((time.perf_counter() - t) * 1000, 2), nseg()))
    print('A per-update ms (n, ms, segments):')
    print(json.dumps(A))
    ms = sorted(x[1] for x in A)
    print('A updates', UPDATES, 'median ms', ms[len(ms) // 2], 'mean ms', round(sum(ms) / len(ms), 2), 'p99 ms', ms[min(len(ms) - 1, int(len(ms) * 0.99))], 'over50ms', sum(1 for m in ms if m > 50), 'top5', [x for x in sorted(A, key=lambda x: -x[1])[:5]])
    db.close()
    # Experiment B: restart, 6 queries different X
    db = Database(d)
    q = '[%s]' % ','.join('%.4f' % rnd.random() for _ in range(D))
    B = []
    for x in range(6):
        t = time.perf_counter()
        r = run(db, "SELECT id FROM t WHERE c=%d ORDER BY L2_DISTANCE(v,'%s') LIMIT 10" % (x, q))
        B.append((x + 1, round((time.perf_counter() - t) * 1000, 2), len(r)))
    print('B per-query ms (n, ms, rows):')
    print(json.dumps(B))
    db.close()
