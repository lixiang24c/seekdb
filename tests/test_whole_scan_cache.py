import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from minivectordb.core import Database
from minivectordb.sql import SQL

class WholeScanCacheTests(unittest.TestCase):
    def test_projection_keeps_vectors_only_when_required(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
            sql.execute('INSERT INTO t VALUES (1,[0,0]),(2,[3,4])');sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            with patch.object(db,'_read_record',side_effect=AssertionError('unnecessary base row read')):
                self.assertEqual(list(sql.execute('SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2')['rows']),[[1,0.],[2,5.]])
            with patch.object(db,'_read_record',wraps=db._read_record) as reader:
                actual=list(sql.execute('SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2')['rows'])
                self.assertEqual(actual,[[1,0.],[2,5.]])
                self.assertTrue(all(call.kwargs['scalar_only'] for call in reader.call_args_list))
            actual=list(sql.execute('SELECT id,v,L2_DISTANCE(v,[3,4]) AS other FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2')['rows'])
            self.assertEqual(actual,[[1,[0.,0.],5.],[2,[3.,4.],0.]])
            self.assertEqual(len(list(sql.execute('SELECT * FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2')['rows'])[0]),2)
            db.close()
    def test_lru_budgets_version_invalidation_and_close(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.SCAN_MAP_LIMIT=4000;db.QUERY_MASK_LIMIT=1200;sql=SQL(db)
            for number in range(3):
                table='t'+str(number)
                sql.execute(f'CREATE TABLE {table}(id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))')
                db.insert(table,({'id':i,'tag':'a' if i%2==0 else 'b','v':[float(i),0.]} for i in range(100)))
                sql.execute(f'CREATE VECTOR INDEX ix{number} ON {table}(v)')
                self.assertEqual(list(sql.execute(f"SELECT id FROM {table} WHERE tag='a' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1")['rows']),[[0]])
                self.assertLessEqual(db._scan_map_bytes,db.SCAN_MAP_LIMIT)
                self.assertLessEqual(db._query_mask_bytes,db.QUERY_MASK_LIMIT)
            query="SELECT id FROM t0 WHERE tag='a' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1"
            db.begin();sql.execute("UPDATE t0 SET tag='b' WHERE id=0")
            self.assertEqual(list(sql.execute(query)['rows']),[[2]])
            db.rollback();self.assertEqual(list(sql.execute(query)['rows']),[[0]])
            huge='x'*4096
            self.assertEqual(list(sql.execute("SELECT id FROM t0 WHERE tag='"+huge+"' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1")['rows']),[])
            self.assertFalse(any(any(isinstance(value,str) and len(value)>1024 for term in key[1] for value in term) for key in db._query_masks))
            before=len(os.listdir('/proc/self/fd'))
            for _ in range(100):self.assertEqual(list(sql.execute(query)['rows']),[[0]])
            self.assertEqual(len(os.listdir('/proc/self/fd')),before)
            db.close();self.assertEqual(db._scan_map_bytes,0);self.assertEqual(db._query_mask_bytes,0);self.assertEqual(db._filter_cache_bytes,0)
    def test_large_mask_and_no_native_fallbacks(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.WHOLE_MASK_ROWS=16;sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, v VECTOR(1))')
            db.insert('t',({'id':i,'tag':i%3,'v':[float(i)]} for i in range(2100)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            query='SELECT id FROM t WHERE tag=1 ORDER BY L2_DISTANCE(v,[0]) LIMIT 3'
            self.assertEqual(list(sql.execute(query)['rows']),[[1],[4],[7]])
            with patch('minivectordb.native.available',return_value=False):
                self.assertEqual(list(sql.execute(query)['rows']),[[1],[4],[7]])
            db.close()

@unittest.skipUnless((Path(__file__).resolve().parents[1]/'minivectordb/libminivector.so').exists(),'native library not built')
class BoundedDistanceTests(unittest.TestCase):
    def test_strict_partial_bound_keeps_ties_and_checks_tail(self):
        import numpy as np
        from minivectordb.native import scan
        data=np.zeros(4100,dtype=[('pk','<i8'),('offset','<u8'),('vector','<f4',(128,))])
        data['pk']=np.arange(4100)[::-1];data['vector'][:,0]=1
        data['vector'][0,127]=1
        query=np.zeros(128,dtype=np.float64)
        for simd in (False,True):
            actual=list(scan(data,query,slice(None),5,use_simd=simd))
            self.assertEqual([r[1] for r in actual],[0,1,2,3,4]);self.assertEqual([r[0] for r in actual],[1.]*5)
        small=data[:3].copy();small['pk']=[100,0,1];small['vector'][:]=0;small['vector'][:,0]=1;small['vector'][1,127]=1
        for simd in (False,True):self.assertEqual(list(scan(small,query,slice(None),1,use_simd=simd))[0][1],1)
if __name__=='__main__':unittest.main()
