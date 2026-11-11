#!/usr/bin/env python3
"""Small end-to-end comparison of common operations; each image builds its own data."""
import json
from pathlib import Path
import random
import sys
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session


def main():
    out = ROOT / 'protocol-results/general-optimization'
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / 'protocol_check/config.json').read_text())
    rng = random.Random(17)
    vectors = [[rng.randrange(-32, 33) / 8 for _ in range(128)] for _ in range(10000)]
    report = {'rows': 10000, 'dimensions': 128, 'point_queries': 100,
              'point_updates': 20, 'topk_queries': 40, 'filtered_queries': 20,
              'scope': 'single local run, independent identical fixtures, Docker 2 CPU / 2 GiB', 'runs': {}}
    baseline = None
    for label, image in [('baseline', 'minivectordb:general-baseline-77808'), ('candidate', 'minivectordb:query-candidate')]:
        s = Session(cfg, out / (label + '.stderr'), image=image)
        answers, times = [], {}
        def query(sql):
            result = s.request(sql)
            assert result['ok'], result
            result.pop('id', None)
            return result
        def measured(name, statements):
            start = time.perf_counter()
            for sql in statements: answers.append(query(sql))
            times[name] = time.perf_counter() - start
        try:
            s.start()
            query('CREATE TABLE t(c BIGINT,id BIGINT PRIMARY KEY,body VARCHAR(1024),v VECTOR(128))')
            measured('load_s', ['INSERT INTO t VALUES ' + ','.join(
                f"({i%10},{i-5000},'{('x'*512)}','{json.dumps(vectors[i])}')" for i in range(start,start+100))
                for start in range(0,10000,100)])
            measured('index_s', ['CREATE VECTOR INDEX ix ON t(v)'])
            measured('point_select_s', [f'SELECT id,c FROM t WHERE id={i*97-5000}' for i in range(100)])
            measured('point_update_s', [f'UPDATE t SET c=9 WHERE id={4900+i}' for i in range(20)])
            measured('topk_s', [f"SELECT id,L2_DISTANCE(v,'{json.dumps(vectors[i*127])}') AS d FROM t " +
                      ('WHERE c=3 ' if i%2 else '') + 'ORDER BY d LIMIT 10' for i in range(40)])
            measured('conditional_mutations_s', ["UPDATE t SET body='updated' WHERE c=8", 'DELETE FROM t WHERE c=7'])
            answers.append(query('SELECT id FROM t WHERE c=8 ORDER BY id'))
            query('BEGIN')
            query("UPDATE t SET body='changed' WHERE id=-5000")
            assert query('SELECT body FROM t WHERE id=-5000')['rows'] == [['changed']]
            query('DELETE FROM t WHERE id=-4999')
            assert query('SELECT id FROM t WHERE id=-4999')['rows'] == []
            query('ROLLBACK')
            assert query('SELECT body FROM t WHERE id=-5000')['rows'] == [['x'*512]]
            s.shutdown(); s.start()
            assert query('SELECT c FROM t WHERE id=4900')['rows'] == [[9]]
            if baseline is None: baseline = answers
            else: assert answers == baseline
            report['runs'][label] = times
        finally: s.close()
    report['speedup'] = {k: report['runs']['baseline'][k]/report['runs']['candidate'][k] for k in times}
    report['correctness'] = 'All 264 collected responses identical; point transaction rollback and committed restart checked per image'
    (out/'comparison.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
if __name__ == '__main__': main()
