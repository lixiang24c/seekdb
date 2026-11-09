import tempfile
import unittest
from unittest.mock import patch
from minivectordb.core import Database,DBError
from minivectordb.sql import SQL

class SecondOptimizationTests(unittest.TestCase):
    def test_large_key_batch_retains_external_sort_and_atomicity(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',1]},'id')
            db.insert('t',({'id':i,'v':[float(i)]} for i in range(4200,-1,-1)))
            db.begin()
            with self.assertRaises(DBError):db.insert('t',[{'id':4201,'v':[0.]},{'id':0,'v':[0.]}])
            db.commit();self.assertEqual(sum(1 for _ in db.rows('t')),4201);db.close()
    def test_mapped_empty_index_and_distance_projection(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db);sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10')['rows']),[])
            sql.execute('INSERT INTO t VALUES (1,[0,0]),(2,[3,4])')
            result=list(sql.execute('SELECT id,L2_DISTANCE(v,[0,0]) AS a,L2_DISTANCE(v,[3,4]) AS b FROM t ORDER BY a LIMIT 2')['rows'])
            self.assertEqual(result,[[1,0.,5.],[2,5.,0.]])
            # No NumPy must remain a correct supported fallback.
            with patch.dict('sys.modules',{'numpy':None}):
                self.assertEqual([r['id'] for r,d in db.topk('t','v',[0,0],2)],[1,2])
            db.close()
    def test_numeric_fast_token_rejects_bad_arrays(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db);sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
            for value in ('[1 2]','[1,,2]','[1e,2]','[--1,2]'):
                with self.assertRaises(DBError):sql.execute('INSERT INTO t VALUES (1,'+value+')')
            self.assertEqual(list(db.rows('t')),[]);db.close()
if __name__=='__main__':unittest.main()
