import unittest,tempfile
from minivectordb.core import Database,DBError
from minivectordb.sql import SQL

class SQLTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=Database(self.tmp.name);self.sql=SQL(self.db)
  self.sql.execute('CREATE TABLE docs(id BIGINT PRIMARY KEY, tag VARCHAR(5), vec VECTOR(2));')
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def rows(self,text):return list(self.sql.execute(text)['rows'])
 def test_end_to_end(self):
  self.sql.execute("INSERT INTO docs VALUES (3,'中',[1,0]),(1,'文',[0,0]),(2,'中',[-1,0]);")
  self.assertEqual(self.rows("SELECT id,L2_DISTANCE(vec,[0,0]) AS d FROM docs WHERE tag='中' ORDER BY d LIMIT 10"),[[2,1.0],[3,1.0]])
  self.sql.execute('BEGIN');self.sql.execute('UPDATE docs SET vec=[0,0] WHERE id=3')
  self.assertEqual(self.rows('SELECT id FROM docs ORDER BY L2_DISTANCE(vec,[0,0]) LIMIT 2'),[[1],[3]])
  self.sql.execute('DELETE FROM docs WHERE id=1');self.sql.execute('ROLLBACK')
  self.assertEqual(self.rows('SELECT id FROM docs ORDER BY id DESC'),[[3],[2],[1]])
 def test_failed_tail_never_executes(self):
  with self.assertRaises(DBError):self.sql.execute("INSERT INTO docs VALUES (1,'a',[1,2]) GARBAGE")
  self.assertEqual(self.rows('SELECT * FROM docs'),[])
 def test_multiline_unicode_and_quotes(self):
  self.sql.execute("INSERT INTO docs(tag,id,vec) VALUES ('a''b',1,[0,0])")
  self.assertEqual(self.rows("SELECT tag FROM docs WHERE id>=1 AND id<2"),[["a'b"]])
 def test_invalid_update_atomicity(self):
  self.sql.execute("INSERT INTO docs VALUES (1,'a',[0,0])")
  self.sql.execute('BEGIN')
  with self.assertRaises(DBError):self.sql.execute("UPDATE docs SET tag='abcdef'")
  self.sql.execute('COMMIT')
  self.assertEqual(self.rows('SELECT tag FROM docs'),[['a']])
 def test_external_sort(self):
  key=lambda row:row['id']
  rows=({'id':i,'payload':'x'*10000} for i in range(400,-1,-1))
  self.assertEqual([r['id'] for r in self.sql.external_sort(rows,key)],list(range(401)))

if __name__=='__main__':unittest.main()
