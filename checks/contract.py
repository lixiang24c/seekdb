#!/usr/bin/env python3
"""Named, hand-calculated contract checks over the official container transport."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'protocol_check'))
from _transport import Session
from _assertions import assert_response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    config = json.loads((ROOT / 'protocol_check/config.json').read_text())
    report = {'image': args.image, 'scope': 'hand-calculated contract examples; not hidden cases', 'checks': []}
    session = Session(config, args.output / 'stderr.txt', image=args.image)
    def save():
        (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    def check(area, sql, expected=None, mode='exact'):
        actual = session.request(sql, mode=mode)
        item = {'area': area, 'sql': sql, 'mode': mode, 'expected': expected or {}, 'actual': actual}
        report['checks'].append(item)
        try:
            assert_response(actual, expected or {}, config)
            item['status'] = 'PASS'
        except Exception as error:
            item.update(status='FAIL', error=str(error))
            raise
        finally:
            save()
    def reject(area, sql): check(area, sql, {'error': True})
    try:
        session.start()
        for ddl in (
            'CREATE TABLE bad(id BIGINT)',
            'CREATE TABLE bad(id VARCHAR(4) PRIMARY KEY)',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(0))',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,s VARCHAR(-1))',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,v VECTOR(2),w VECTOR(2))',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,x BIGINT PRIMARY KEY)',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,id BIGINT)',
            'CREATE TABLE bad(id BIGINT PRIMARY KEY,x FLOAT)'):
            reject('schema', ddl)
        check('schema', 'CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT,s VARCHAR(2),v VECTOR(2))')
        reject('schema', 'CREATE TABLE t(id BIGINT PRIMARY KEY)')
        check('insert', "INSERT INTO t(v,s,n,id) VALUES('[3,4]','😀中',5,3),('[0,0]','é',5,1),('[-3,-4]','é',7,2)", {'affected_rows': 3})
        check('projection', 'SELECT * FROM t ORDER BY id', {'columns': ['id','n','s','v'], 'rows': [[1,5,'é',[0.,0.]],[2,7,'é',[-3.,-4.]],[3,5,'😀中',[3.,4.]]]})
        for value in ('9223372036854775808','-9223372036854775809','1.5','NULL'):
            reject('bigint', f'UPDATE t SET n={value} WHERE id=999')
        reject('unicode', "UPDATE t SET s='abc' WHERE id=999")
        for vector in ('[NaN,0]','[Infinity,0]','[1e39,0]','[1]','[0,0,0]','[true,0]','[null,0]'):
            reject('float32', f"UPDATE t SET v='{vector}' WHERE id=999")
        for op, ids in [('=',[1,3]),('<',[1,3]),('<=',[1,3]),('>',[2]),('>=',[2])]:
            bound = 6 if op in ('<','>') else 5 if op in ('=','<=') else 7
            check('comparisons', f'SELECT id FROM t WHERE n{op}{bound} ORDER BY id', {'columns':['id'], 'rows':[[i] for i in ids]})
        check('and', 'SELECT id FROM t WHERE n=5 AND id>1 ORDER BY id', {'rows':[[3]]})
        check('unicode_order', 'SELECT id FROM t ORDER BY s', {'rows':[[2],[1],[3]]})
        check('tie_order', 'SELECT id FROM t ORDER BY n DESC', {'rows':[[2],[1],[3]]})
        check('zero_limit', 'SELECT id FROM t ORDER BY n LIMIT 0', {'columns':['id'],'rows':[]})
        query = "SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM t WHERE n=5 ORDER BY d LIMIT 10"
        check('filtered_topk_scan', query, {'columns':['id','d'],'rows':[[1,0.],[3,5.]]})
        check('index', 'CREATE VECTOR INDEX ix ON t(v)')
        check('filtered_topk_index', query, {'columns':['id','d'],'rows':[[1,0.],[3,5.]]})
        check('distance_ties', "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10", {'rows':[[1],[2],[3]]})
        check('filter_before_topk', "SELECT id FROM t WHERE n=7 ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1", {'rows':[[2]]})
        check('empty_topk', "SELECT id FROM t WHERE n=999 ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10", {'columns':['id'],'rows':[]})
        check('ann_filtered', query, {'rows':[[1,0.],[3,5.]]}, mode='ann')
        check('ann_unfiltered', "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 10", {'rows':[[1],[2],[3]]}, mode='ann')
        check('bigint_bounds', 'CREATE TABLE limits(id BIGINT PRIMARY KEY,s VARCHAR(32))')
        check('bigint_bounds', "INSERT INTO limits VALUES(-9223372036854775808,'a''b;--中'),(9223372036854775807,'ok')", {'affected_rows':2})
        check('bigint_bounds', 'SELECT * FROM limits ORDER BY id', {'columns':['id','s'],'rows':[[-9223372036854775808,"a'b;--中"],[9223372036854775807,'ok']]})
        check('transaction', 'BEGIN')
        check('transaction', "UPDATE t SET v='[0,0]',n=5 WHERE id=2", {'affected_rows':1})
        check('transaction_visibility', query, {'rows':[[1,0.],[2,0.],[3,5.]]})
        reject('statement_atomicity', "INSERT INTO t VALUES(4,0,'ok','[0,0]'),(1,0,'ok','[0,0]')")
        check('statement_atomicity', 'SELECT id FROM t ORDER BY id', {'rows':[[1],[2],[3]]})
        check('transaction', 'COMMIT')
        check('transaction', 'ROLLBACK')
        session.crash(); session.start()
        check('confirmed_commit_recovery', query, {'rows':[[1,0.],[2,0.],[3,5.]]})
        check('transaction', 'BEGIN')
        check('transaction', 'DELETE FROM t WHERE id<=2', {'affected_rows':2})
        check('delete_visibility', query, {'rows':[[3,5.]]})
        session.crash(); session.start()
        check('uncommitted_recovery', query, {'rows':[[1,0.],[2,0.],[3,5.]]})
        for sql in ('UPDATE t SET id=10 WHERE id=1', 'DELETE FROM t WHERE id=1 OR id=2',
                    'DELETE FROM t LIMIT 1', 'SELECT COUNT(*) FROM t',
                    'SELECT * FROM t JOIN t', 'ALTER TABLE t ADD x BIGINT',
                    'DELETE FROM t; DELETE FROM t', 'DELETE FROM t garbage'):
            reject('unsupported_sql', sql)
            check('unsupported_atomicity', 'SELECT id FROM t ORDER BY id', {'rows':[[1],[2],[3]]})
        check('float32_storage', "UPDATE t SET v='[16777217,0]' WHERE id=3", {'affected_rows':1})
        check('float32_storage', "SELECT v,L2_DISTANCE(v,'[16777216,0]') AS d FROM t WHERE id=3", {'rows':[[[16777216.,0.],0.]]})
        check('cache_invalid_binding', "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0,0]') LIMIT 1", {'rows':[[1]]})
        reject('cache_invalid_binding', "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[NaN,0]') LIMIT 1")
        check('cache_rebind', "SELECT id FROM t ORDER BY L2_DISTANCE(v,'[16777216,0]') LIMIT 1", {'rows':[[3]]})
        session.shutdown(); session.start()
        check('normal_restart', 'SELECT id FROM t ORDER BY id', {'rows':[[1],[2],[3]]})
        session.shutdown()
        report['status']='PASS'
    except Exception as error:
        report.update(status='FAIL', error=repr(error))
        raise
    finally:
        session.close(); save()
    print(json.dumps({'status':report['status'],'checks':len(report['checks'])}))


if __name__ == '__main__': main()
