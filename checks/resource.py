#!/usr/bin/env python3
"""Real image, 64 MiB container, persisted data larger than that memory budget."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='minivectordb:rewrite-complete')
    args = ap.parse_args()
    out = ROOT / 'protocol-results/resource'
    out.mkdir(parents=True, exist_ok=True)
    config = json.loads((ROOT / 'protocol_check/config.json').read_text())
    config['memory'] = '64m'
    s = Session(config, out / 'stderr.log', image=args.image)
    report = {'memory': '64m', 'limit_source': 'local stress condition; official public config is 2g', 'stages': []}
    start = time.monotonic()
    def stage(name, details=None):
        item = {'stage': name, 'elapsed': round(time.monotonic() - start, 3), 'details': details}
        report['stages'].append(item)
        (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(item), flush=True)
    def sql(q, mode='exact'):
        result = s.request(q, mode=mode)
        assert result['ok'], result
        return result
    try:
        s.start()
        sql('CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(12000),e VECTOR(2))')
        text = 'x' * 8192
        for base in range(0, 9000, 100):
            rows = ','.join(f"({i},'{text}','[0,0]')" for i in range(base, base + 100))
            sql('INSERT INTO t VALUES ' + rows)
        size = int(s.docker('exec', s.name, 'du', '-sb', '/data').stdout.split()[0])
        assert size > 64 * 1024 * 1024, size
        stage('load_beyond_memory', {'rows': 9000, 'data_bytes': size})
        sql('CREATE VECTOR INDEX ix ON t(e)')
        q = "SELECT id,L2_DISTANCE(e,'[1,0]') AS d FROM t ORDER BY d LIMIT 10"
        for mode in ('exact', 'ann'):
            assert sql(q, mode)['rows'] == [[i, 1.0] for i in range(10)]
        stage('index_and_exact_ann_distance_projection', 'PASS')
        assert sql("UPDATE t SET s='" + 'y' * 8192 + "'")['affected_rows'] == 9000
        stage('full_table_update', 'PASS')
        # Sorting all matching rows must spill to /data, regardless of the small output projection.
        rows = sql('SELECT id FROM t ORDER BY s DESC,id DESC')['rows']
        assert rows == [[i] for i in reversed(range(9000))]
        stage('external_sort', 'PASS')
        sql('BEGIN'); sql("UPDATE t SET e='[3,4]' WHERE id<100")
        assert sql("SELECT id FROM t WHERE id<100 ORDER BY L2_DISTANCE(e,'[3,4]') LIMIT 3", 'ann')['rows'] == [[0], [1], [2]]
        sql('ROLLBACK')
        s.crash(); s.start()
        assert sql(q, 'ann')['rows'] == [[i, 1.0] for i in range(10)]
        assert len(sql('SELECT id FROM t')['rows']) == 9000
        peak = s.docker('exec', s.name, 'cat', '/sys/fs/cgroup/memory.peak').stdout.decode().strip()
        stage('rollback_crash_restart', {'status': 'PASS', 'restart_memory_peak_bytes': int(peak)})
        s.shutdown()
        report['status'] = 'PASS'
        stage('complete')
    finally:
        s.close()


if __name__ == '__main__': main()
