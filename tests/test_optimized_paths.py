import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from minivectordb.core import Database,DBError
from minivectordb.sql import SQL
from minivectordb.server import statements

class OptimizedPathTests(unittest.TestCase):
    def test_legacy_snapshot_reads_and_upgrades(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            (path/'table-legacy.jsonl').write_text('{"id":1,"label":"中文","v":[1.0,-2.0]}\n')
            (path/'keys-legacy.bin').write_bytes(struct.pack('<q',1))
            (path/'manifest.json').write_text(json.dumps({'t':{'columns':{'id':['BIGINT'],'label':['VARCHAR',4],'v':['VECTOR',2]},'primary':'id','file':'table-legacy.jsonl','keys':'keys-legacy.bin'}}))
            db=Database(root);sql=SQL(db)
            self.assertEqual(list(sql.execute('SELECT label FROM t')['rows']),[['中文']])
            sql.execute("INSERT INTO t VALUES (2,'🙂',[.5,-.5])")
            sql.execute('CREATE VECTOR INDEX ix ON t(v)');db.close();db=Database(root);sql=SQL(db)
            self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 10')['rows']),[[2],[1]])
            db.close()
    def test_cached_filters_ties_and_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(4), v VECTOR(2))')
            db.insert('t',({'id':i,'tag':'中文' if i%2 else '🙂','v':[1.,0.]} for i in range(2100)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            query="SELECT id FROM t WHERE tag='中文' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 5"
            for _ in range(3):self.assertEqual(list(sql.execute(query)['rows']),[[1],[3],[5],[7],[9]])
            db.begin();sql.execute("UPDATE t SET tag='🙂' WHERE id<=9")
            self.assertEqual(list(sql.execute(query)['rows']),[[11],[13],[15],[17],[19]])
            db.rollback();self.assertEqual(list(sql.execute(query)['rows']),[[1],[3],[5],[7],[9]])
            self.assertLessEqual(db._filter_cache_bytes,db.FILTER_CACHE_LIMIT)
            for condition,expected in [('id<9223372036854775808',[[0]]),('id>=9223372036854775808',[]),('id>-9223372036854775809',[[0]]),('id=-9223372036854775809',[])]:
                self.assertEqual(list(sql.execute('SELECT id FROM t WHERE '+condition+' ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 1')['rows']),expected)
            db.close()
    def test_vector_literal_fast_path(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db);sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
            sql.execute('INSERT INTO t VALUES (1,[+ .5,+ 1.])')
            sql.execute('INSERT INTO t VALUES (2,[0,/* bracket ] inside comment */-1])')
            for vector in ('[1,]','[true,0]','[NaN,0]','[1e99,0]','["0",0]','[[1],0]'):
                with self.assertRaises(DBError):sql.execute('INSERT INTO t VALUES (3,'+vector+')')
            self.assertEqual(list(sql.execute('SELECT id,v FROM t ORDER BY id')['rows']),[[1,[.5,1.]],[2,[0.,-1.]]]);db.close()
    def test_filter_cache_budget_falls_back_to_streaming(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag VARCHAR(1024), v VECTOR(1))')
            db.insert('t',({'id':i,'tag':'x'*1024 if i%2 else 'y'*1024,'v':[float(i)]} for i in range(1100)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)');db.FILTER_CACHE_LIMIT=256
            query="SELECT id FROM t WHERE tag<'y' ORDER BY L2_DISTANCE(v,[0]) LIMIT 3"
            for _ in range(2):self.assertEqual(list(sql.execute(query)['rows']),[[1],[3],[5]])
            self.assertEqual(db._filter_cache_bytes,0);db.close()

    def test_primary_runs_compact_and_recover(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT']},'id')
            for i in range(70):db.insert('t',[{'id':i*2},{'id':-i*2-1}])
            db.close();db=Database(root)
            with self.assertRaises(DBError):db.insert('t',[{'id':1000},{'id':-67}])
            self.assertEqual(sum(1 for _ in db.rows('t')),140)
            db.modify('t',lambda r:r['id']==-67);db.insert('t',[{'id':-67}])
            self.assertEqual(sum(1 for _ in db.rows('t')),140);db.close()

    def test_float32_storage_drives_exact_order(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, v VECTOR(2))')
            sql.execute('INSERT INTO t VALUES (2,[16777217,0]),(1,[16777216,0]),(3,[16777218,0])')
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            self.assertEqual(list(sql.execute('SELECT id FROM t ORDER BY L2_DISTANCE(v,[16777216,0]) LIMIT 2')['rows']),[[1],[2]])
            db.close()

    def test_statement_boundaries_across_lines(self):
        source="SELECT 'a;\n''b'; /* unclosed;\n closed */ SELECT `a;b`; -- ignored ;\nSELECT 'c';"
        self.assertEqual(list(statements(io.StringIO(source))),["SELECT 'a;\n''b'"," /* unclosed;\n closed */ SELECT `a;b`"," -- ignored ;\nSELECT 'c'"])
if __name__=='__main__':unittest.main()
