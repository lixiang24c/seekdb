import tempfile
import unittest
from unittest.mock import patch
from minivectordb.core import Database,DBError
from minivectordb.sql import SQL


class CompetitionContractTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.db=Database(self.directory.name);self.sql=SQL(self.db)
        self.sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,label VARCHAR(16),v VECTOR(2))')
    def tearDown(self):
        self.db.close();self.directory.cleanup()
    def rows(self,query):return list(self.sql.execute(query)['rows'])
    def test_quoted_vectors_and_array_looking_varchar(self):
        self.sql.execute("INSERT INTO t VALUES(1,'[0,0]','[3,4]'),(2,'[1,1]','[0,0]')")
        self.sql.execute('CREATE VECTOR INDEX ix ON t(v)')
        query="SELECT id,L2_DISTANCE(v,'{}') AS d FROM t WHERE label='[0,0]' ORDER BY d LIMIT 2"
        self.assertEqual(self.rows(query.format('[0,0]')),[[1,5.]])
        with patch.object(self.sql,'parse',side_effect=AssertionError('quoted query plan not reused')):
            self.assertEqual(self.rows(query.format('[3,4]')),[[1,0.]])
        self.assertEqual(self.rows(query.format('[0,0]').replace("label='[0,0]'","label='[1,1]'")),[[2,0.]])
        self.db.begin();self.sql.execute("UPDATE t SET v='[0,0]' WHERE id=1")
        self.assertEqual(self.rows(query.format('[0,0]')),[[1,0.]])
        self.db.rollback();self.assertEqual(self.rows(query.format('[0,0]')),[[1,5.]])
    def test_invalid_quoted_vectors_preserve_prior_transaction_statements(self):
        self.db.begin();self.sql.execute("INSERT INTO t VALUES(1,'ok','[0,0]')")
        for invalid in ('[NaN,0]','[Infinity,0]','[1e99,0]','[true,0]','[null,0]','[1]','[[1],0]','{"a":1}','[1,]'):
            with self.assertRaises(DBError):self.sql.execute("INSERT INTO t VALUES(2,'ok','[1,1]'),(3,'bad','"+invalid+"')")
            with self.assertRaises(DBError):self.rows("SELECT id FROM t ORDER BY L2_DISTANCE(v,'"+invalid+"') LIMIT 1")
        self.db.commit();self.assertEqual(self.rows('SELECT id FROM t'),[[1]])
    def test_unsupported_sql_does_not_modify_records(self):
        self.sql.execute("INSERT INTO t VALUES(1,'ok','[0,0]')")
        for query in ("DELETE FROM t WHERE id=1 OR id=2","UPDATE t SET id=9 WHERE id=1",
                      'ALTER TABLE t ADD x BIGINT','SELECT COUNT(*) FROM t',
                      'DELETE FROM t LIMIT 1','INSERT INTO t VALUES(2,NULL,\'[0,0]\')'):
            with self.assertRaises(DBError):self.sql.execute(query)
        self.assertEqual(self.rows('SELECT id FROM t'),[[1]])

if __name__=='__main__':unittest.main()
