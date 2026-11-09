import tempfile
import unittest
from unittest.mock import patch
from minivectordb.core import Database,DBError
from minivectordb.sql import SQL


class QueryPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.db=Database(self.directory.name);self.sql=SQL(self.db)
        self.sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.sql.execute('INSERT INTO t VALUES (1,[0,0]),(2,[3,4])')
        self.sql.execute('CREATE VECTOR INDEX ix ON t(v)')
    def tearDown(self):
        self.db.close();self.directory.cleanup()
    def rows(self,query):return list(self.sql.execute(query)['rows'])
    def test_new_vectors_reuse_plan_and_validate_each_binding(self):
        query='SELECT id,L2_DISTANCE(v,{}) AS d FROM t ORDER BY d LIMIT 1'
        self.assertEqual(self.rows(query.format('[0,0]')),[[1,0.]])
        with patch.object(self.sql,'parse',side_effect=AssertionError('plan not reused')):
            self.assertEqual(self.rows(query.format('[3,4]')),[[2,0.]])
            for vector in ('[0]','[1e99,0]','[true,0]','[1,]'):
                with self.assertRaises(DBError):self.rows(query.format(vector))
            self.assertEqual(self.rows(query.format('[0,0]')),[[1,0.]])
        self.assertEqual(len(self.sql._plans),1)
    def test_multiple_parameters_and_lazy_results_keep_their_bindings(self):
        query='SELECT id,L2_DISTANCE(v,{}) AS a,L2_DISTANCE(v,{}) AS b FROM t ORDER BY a LIMIT 1'
        pending=self.sql.execute(query.format('[0,0]','[3,4]'))
        self.assertEqual(self.rows(query.format('[3,4]','[0,0]')),[[2,0.,5.]])
        self.assertEqual(list(pending['rows']),[[1,0.,5.]])
    def test_cached_plan_reads_transaction_state_and_rejects_trailing_tokens(self):
        query='SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1'
        self.assertEqual(self.rows(query),[[1]])
        self.db.begin();self.sql.execute('DELETE FROM t WHERE id=1')
        self.assertEqual(self.rows(query),[[2]])
        self.db.rollback();self.assertEqual(self.rows(query),[[1]])
        self.db.begin();self.sql.execute('CREATE TABLE temp(id BIGINT PRIMARY KEY,v VECTOR(2))')
        self.assertEqual(self.rows(query.replace('FROM t ','FROM temp ')),[])
        self.db.rollback()
        with self.assertRaises(DBError):self.rows(query.replace('FROM t ','FROM temp '))
        with self.assertRaises(DBError):self.rows(query+' garbage')
    def test_plan_budget_and_comment_literals(self):
        self.sql.PLAN_CACHE_LIMIT=12000
        for limit in range(20):
            self.rows('SELECT id FROM t ORDER BY L2_DISTANCE(v,[+ 0,/* ] */0]) LIMIT '+str(limit))
            self.assertLessEqual(self.sql._plan_bytes,self.sql.PLAN_CACHE_LIMIT)
        self.assertLessEqual(len(self.sql._plans),2)
        with self.assertRaises(DBError):self.rows('SELECT id FROM t WHERE id=[0,0]')

if __name__=='__main__':unittest.main()
