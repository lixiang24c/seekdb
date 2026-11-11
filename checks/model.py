#!/usr/bin/env python3
"""Independent state-machine oracle across SQL, indexes, transactions and restarts."""
import copy
import argparse
import json
import math
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='minivectordb:rewrite-complete')
    parser.add_argument('--output', default='protocol-results/model')
    parser.add_argument('--seed', type=int, default=20261111)
    parser.add_argument('--dimensions', type=int, default=4)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    out = ROOT / args.output
    out.mkdir(parents=True, exist_ok=True)
    s = Session(json.loads((ROOT / 'protocol_check/config.json').read_text()), out/'stderr.log',
                image=args.image)
    report = {'seed': args.seed, 'dimensions':args.dimensions, 'steps': [], 'queries_checked': 0}
    current, committed, active = {}, {}, False
    def request(q, mode='exact', error=False):
        r = s.request(q, mode=mode)
        assert r['ok'] != error, (q, r)
        return r
    try:
        s.start()
        request(f'CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR({args.dimensions}))')
        request('CREATE VECTOR INDEX ix ON t(v)')
        for step in range(250):
            op = rng.choice(['insert', 'insert', 'update', 'delete', 'begin', 'commit', 'rollback', 'crash'])
            if op == 'begin':
                request('BEGIN', error=active)
                active = True
            elif op == 'commit':
                request('COMMIT'); committed = copy.deepcopy(current); active = False
            elif op == 'rollback':
                request('ROLLBACK'); current = copy.deepcopy(committed); active = False
            elif op == 'crash':
                s.crash(); s.start(); current = copy.deepcopy(committed); active = False
            elif op == 'insert':
                ident = rng.randrange(-20, 50)
                n = rng.randrange(5)
                v = [rng.randrange(-5, 6) for _ in range(args.dimensions)]
                error = ident in current
                request(f"INSERT INTO t VALUES({ident},{n},'{json.dumps(v)}')", error=error)
                if not error: current[ident] = (n, v)
            elif op == 'update':
                bound = rng.randrange(-20, 50); n = rng.randrange(5); v = [rng.randrange(-5, 6) for _ in range(args.dimensions)]
                r = request(f"UPDATE t SET n={n},v='{json.dumps(v)}' WHERE id<={bound}")
                ids = [i for i in current if i <= bound]
                assert r['affected_rows'] == len(ids)
                for i in ids: current[i] = (n, v[:])
            elif op == 'delete':
                n = rng.randrange(5)
                r = request(f'DELETE FROM t WHERE n={n}')
                ids = [i for i in current if current[i][0] == n]
                assert r['affected_rows'] == len(ids)
                for i in ids: del current[i]
            if not active: committed = copy.deepcopy(current)
            expected = [[i, current[i][0], current[i][1]] for i in sorted(current)]
            assert request('SELECT * FROM t ORDER BY id')['rows'] == expected
            q = [rng.randrange(-5, 6) for _ in range(args.dimensions)]
            limit = rng.randrange(0, 10); group = rng.randrange(5)
            rows = [(sum((x-y)**2 for x,y in zip(v,q)), i) for i,(n,v) in current.items() if n <= group]
            truth = [[i, math.sqrt(d)] for d,i in sorted(rows)[:limit]]
            sql = f"SELECT id,L2_DISTANCE(v,'{json.dumps(q)}') AS d FROM t WHERE n<={group} ORDER BY d LIMIT {limit}"
            for mode in ('exact', 'ann'):
                assert request(sql, mode)['rows'] == truth, (sql, truth)
            report['queries_checked'] += 3
            report['steps'].append({'step': step, 'operation': op, 'rows': len(current), 'in_transaction': active})
        s.shutdown()
        report['status'] = 'PASS'
    finally:
        s.close()
        (out/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'status': report['status'], 'operations': 250, 'oracle_queries': report['queries_checked']}))


if __name__ == '__main__': main()
