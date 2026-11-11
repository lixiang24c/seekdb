#!/usr/bin/env python3
"""End-to-end bounded range and reverse scans against the preceding image."""
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session
out=ROOT/'protocol-results/ranges';out.mkdir(parents=True,exist_ok=True)
config=json.loads((ROOT/'protocol_check/config.json').read_text())
queries=[]
for i in range(30):
    lo=8000+i*13
    for direction in ('ASC','DESC'):
        queries.append((f'SELECT id FROM t WHERE id>={lo} AND id<{lo+40} ORDER BY id {direction} LIMIT 10', [[k] for k in sorted(range(lo,lo+40),reverse=direction=='DESC')[:10]]))
queries += [('SELECT id FROM t WHERE id>9223372036854775807',[]),('SELECT id FROM t WHERE id<-9223372036854775808',[]),('SELECT id FROM t WHERE id>8 AND id<3',[]),('SELECT id FROM t ORDER BY id DESC LIMIT 3',[[9999],[9998],[9997]])]
answers=None;report={'rows':10000,'range_queries':len(queries),'scope':'Local Docker 2 CPU / 2 GiB, single run, identical independent fixtures','runs':{}}
for name,image in [('baseline','minivectordb:range-baseline-da708fc6'),('candidate','minivectordb:range-candidate')]:
    s=Session(config,out/(name+'.stderr'),image=image)
    def q(sql):
        r=s.request(sql); assert r['ok'],r
        return r
    try:
        s.start();q('CREATE TABLE t(c BIGINT,id BIGINT PRIMARY KEY,v VECTOR(4))')
        for start in range(0,10000,100):
            q('INSERT INTO t VALUES '+','.join(f"({i%7},{i},'[{i%11},1,2,3]')" for i in range(start,start+100)))
        q('CREATE VECTOR INDEX ix ON t(v)')
        start=time.perf_counter()
        for sql,expected in queries: assert q(sql)['rows']==expected
        elapsed=time.perf_counter()-start
        sqls=["SELECT id,L2_DISTANCE(v,'[1,1,2,3]') AS d FROM t WHERE id>=8000 AND id<8100 ORDER BY d LIMIT 10",'SELECT id,c FROM t WHERE id>9000 ORDER BY c DESC,id LIMIT 30', 'SELECT id FROM t ORDER BY c,id DESC LIMIT 30']
        got=[q(sql)['rows'] for sql in sqls]
        if answers is None: answers=got
        else: assert got==answers
        q('BEGIN');q('DELETE FROM t WHERE id>=9995');assert q('SELECT id FROM t ORDER BY id DESC LIMIT 1')['rows']==[[9994]]
        q('ROLLBACK');assert q('SELECT id FROM t ORDER BY id DESC LIMIT 1')['rows']==[[9999]]
        start=time.perf_counter()
        for _ in range(20): assert q('UPDATE t SET c=3 WHERE id=9999')['affected_rows']==1
        noop=time.perf_counter()-start
        start=time.perf_counter()
        for _ in range(30): q("SELECT id,L2_DISTANCE(v,'[1,1,2,3]') AS d FROM t ORDER BY d LIMIT 10")
        topk=time.perf_counter()-start
        s.shutdown();s.start();assert q('SELECT id FROM t ORDER BY id DESC LIMIT 1')['rows']==[[9999]]
        report['runs'][name]={'ranges_s':elapsed,'same_value_updates_s':noop,'topk_s':topk}
    finally:s.close()
report['speedup']={k:report['runs']['baseline'][k]/report['runs']['candidate'][k] for k in report['runs']['candidate']}
report['correctness']='64 range/order outputs checked independently per image; three other sort/filtered-vector responses compared; delete/rollback/restart checked'
(out/'result.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
