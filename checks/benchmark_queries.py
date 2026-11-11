#!/usr/bin/env python3
"""Compare two actual images against one unchanged data volume.

Build fc2567d9 as minivectordb:query-baseline-fc2567d9, and the current
checkout as minivectordb:query-candidate before running this script.
"""
import json
import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', default='minivectordb:query-baseline-fc2567d9')
    parser.add_argument('--candidate', default='minivectordb:query-candidate')
    parser.add_argument('--output', default='protocol-results/query-optimization')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--queries', type=int, default=80)
    parser.add_argument('--seed', type=int, default=20261112)
    parser.add_argument('--filter-categories',type=int,default=1)
    args = parser.parse_args()
    if not 1 <= args.filter_categories <= 10: parser.error('filter-categories must be 1..10')
    out = ROOT / args.output
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / 'protocol_check/config.json').read_text())
    session = Session(config, out / 'benchmark.stderr', image=args.baseline)
    rng = random.Random(args.seed)
    vectors = [[rng.randrange(-64, 65) / 8 for _ in range(128)] for _ in range(10000)]
    query_vectors = [[rng.randrange(-64, 65) / 8 for _ in range(128)] for _ in range(args.queries)]
    statements = [f"SELECT id,L2_DISTANCE(v,'{json.dumps(q)}') AS d FROM t " +
                  (f'WHERE category={3 if args.filter_categories==1 else (i//2)%args.filter_categories} ' if i % 2 else '') + 'ORDER BY d LIMIT 10'
                  for i, q in enumerate(query_vectors)]
    report = {'rows': 10000, 'dimensions': 128, 'queries': args.queries, 'filtered_queries': args.queries//2,
              'filter_categories':args.filter_categories, 'projected_distance': True, 'content_bytes_per_row': 512,
              'cpus': config['cpus'], 'memory': config['memory'], 'seed': args.seed,
              'scope': 'Alternating image order across rounds; local comparison, not formal score', 'rounds':args.rounds, 'samples':{'baseline':[], 'candidate':[]}, 'runs':{}}
    expected = []
    try:
        session.start()
        assert session.request('CREATE TABLE t(id BIGINT PRIMARY KEY,category BIGINT,content VARCHAR(1024),v VECTOR(128))')['ok']
        for start in range(0, 10000, 100):
            rows = ','.join(f"({i},{i%10},'{('x'*512)}','{json.dumps(vectors[i])}')" for i in range(start, start+100))
            assert session.request('INSERT INTO t VALUES ' + rows)['ok']
        assert session.request('CREATE VECTOR INDEX ix ON t(v)')['ok']
        schedule = []
        for round_index in range(args.rounds):
            order = [('baseline', args.baseline), ('candidate', args.candidate)]
            schedule.extend(order if round_index%2 == 0 else order[::-1])
        for pass_index, (label, image) in enumerate(schedule):
            if pass_index:
                session.shutdown(); session.image = image; session.start()
            warmup = []
            for sql in statements[:2]:
                started = time.perf_counter()
                assert session.request(sql)['ok']
                warmup.append(time.perf_counter() - started)
            latency = []
            for i, sql in enumerate(statements):
                started = time.perf_counter()
                result = session.request(sql, mode='ann' if i % 4 < 2 else 'exact')
                latency.append(time.perf_counter() - started)
                assert result['ok'], result
                if pass_index == 0:
                    expected.append(result['rows'])
                    if i < 8:
                        q = query_vectors[i]
                        truth = sorted((math.fsum((x-y)*(x-y) for x,y in zip(vec,q)), j)
                                       for j,vec in enumerate(vectors) if not i%2 or j%10 == (3 if args.filter_categories==1 else (i//2)%args.filter_categories))[:10]
                        assert result['rows'] == [[j,math.sqrt(d)] for d,j in truth]
                else:
                    assert result['rows'] == expected[i], (i, result['rows'], expected[i])
            sample = {'cold_unfiltered_seconds':warmup[0], 'cold_filtered_seconds':warmup[1], 'total_seconds': sum(latency),
                'unfiltered_seconds': sum(latency[::2]), 'filtered_seconds': sum(latency[1::2]),
                'median_ms': statistics.median(latency)*1000, 'max_ms': max(latency)*1000}
            report['samples'][label].append(sample)
            print(json.dumps({label:sample}),flush=True)
        for label, samples in report['samples'].items():
            report['runs'][label] = {key:statistics.median(sample[key] for sample in samples) for key in samples[0]}
        report['speedup'] = report['runs']['baseline']['total_seconds']/report['runs']['candidate']['total_seconds']
        report['correctness'] = f'All {args.queries * len(schedule)} measured responses checked; first 8 independently checked'
        session.shutdown()
    finally:
        session.close()
        (out / 'comparison.json').write_text(json.dumps(report, indent=2)+'\n')


if __name__ == '__main__': main()
