#!/usr/bin/env python3
"""Compare two actual images against one unchanged data volume.

Build fc2567d9 as minivectordb:query-baseline-fc2567d9, and the current
checkout as minivectordb:query-candidate before running this script.
"""
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session


def main():
    out = ROOT / 'protocol-results/query-optimization'
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / 'protocol_check/config.json').read_text())
    session = Session(config, out / 'benchmark.stderr', image='minivectordb:query-baseline-fc2567d9')
    rng = random.Random(20261112)
    vectors = [[rng.randrange(-64, 65) / 8 for _ in range(128)] for _ in range(10000)]
    query_vectors = [[rng.randrange(-64, 65) / 8 for _ in range(128)] for _ in range(80)]
    statements = [f"SELECT id,L2_DISTANCE(v,'{json.dumps(q)}') AS d FROM t " +
                  ('WHERE category=3 ' if i % 2 else '') + 'ORDER BY d LIMIT 10'
                  for i, q in enumerate(query_vectors)]
    report = {'rows': 10000, 'dimensions': 128, 'queries': 80, 'filtered_queries': 40,
              'projected_distance': True, 'content_bytes_per_row': 512,
              'cpus': config['cpus'], 'memory': config['memory'], 'seed': 20261112,
              'scope': 'Local single-run comparison, not the formal benchmark score', 'runs': {}}
    expected = []
    try:
        session.start()
        assert session.request('CREATE TABLE t(id BIGINT PRIMARY KEY,category BIGINT,content VARCHAR(1024),v VECTOR(128))')['ok']
        for start in range(0, 10000, 100):
            rows = ','.join(f"({i},{i%10},'{('x'*512)}','{json.dumps(vectors[i])}')" for i in range(start, start+100))
            assert session.request('INSERT INTO t VALUES ' + rows)['ok']
        assert session.request('CREATE VECTOR INDEX ix ON t(v)')['ok']
        for label, image in [('baseline', 'minivectordb:query-baseline-fc2567d9'), ('candidate', 'minivectordb:query-candidate')]:
            if label == 'candidate':
                session.shutdown(); session.image = image; session.start()
            for sql in statements[:2]: assert session.request(sql)['ok']
            latency = []
            for i, sql in enumerate(statements):
                started = time.perf_counter()
                result = session.request(sql, mode='ann' if i % 4 < 2 else 'exact')
                latency.append(time.perf_counter() - started)
                assert result['ok'], result
                if label == 'baseline':
                    expected.append(result['rows'])
                    if i < 8:
                        q = query_vectors[i]
                        truth = sorted((math.fsum((x-y)*(x-y) for x,y in zip(vec,q)), j)
                                       for j,vec in enumerate(vectors) if not i%2 or j%10 == 3)[:10]
                        assert result['rows'] == [[j,math.sqrt(d)] for d,j in truth]
                else:
                    assert result['rows'] == expected[i], (i, result['rows'], expected[i])
            report['runs'][label] = {'total_seconds': sum(latency),
                'unfiltered_seconds': sum(latency[::2]), 'filtered_seconds': sum(latency[1::2]),
                'median_ms': statistics.median(latency)*1000, 'max_ms': max(latency)*1000}
            print(json.dumps({label: report['runs'][label]}), flush=True)
        report['speedup'] = report['runs']['baseline']['total_seconds']/report['runs']['candidate']['total_seconds']
        report['correctness'] = 'All 80 candidate responses equal baseline; 8 baseline queries independently checked'
        session.shutdown()
    finally:
        session.close()
        (out / 'comparison.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__': main()
