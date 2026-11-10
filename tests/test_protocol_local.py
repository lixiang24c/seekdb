import io,json,subprocess,sys,tempfile,unittest
from minivectordb.server import statements
class StatementTests(unittest.TestCase):
 def test_mode_switch_error_codes_and_transaction_continue_across_restart(self):
  with tempfile.TemporaryDirectory() as root:
   request="""CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(1));
BEGIN;
INSERT INTO t VALUES (1,'[1]');
--mode ann
INSERT INTO t VALUES (2,'[2]'),(1,'[3]');
SELECT id FROM t ORDER BY L2_DISTANCE(v,'[0]') LIMIT 2;
--mode exact
COMMIT;
ALTER TABLE t ADD x BIGINT;
SELECT id FROM t;
"""
   command=[sys.executable,'-m','minivectordb','--data',root,'--protocol','local']
   first=subprocess.run(command,input=request,text=True,capture_output=True,check=True,timeout=15)
   lines=first.stdout.splitlines();self.assertEqual(lines.pop(0),'LOCAL_SQL_READY')
   frames=[json.loads(line) for line in lines]
   self.assertEqual([frame['error_code'] for frame in frames if frame.get('ok') is False],['duplicate_key','unsupported_sql'])
   self.assertEqual([frame['result']['mode'] for frame in frames if isinstance(frame.get('result'),dict) and 'mode' in frame['result']],['ann','exact'])
   self.assertEqual([frame['row'] for frame in frames if 'row' in frame],[[1],[1]])
   second=subprocess.run(command,input='SELECT id FROM t;\n',text=True,capture_output=True,check=True,timeout=15)
   self.assertEqual([json.loads(line)['row'] for line in second.stdout.splitlines()[1:] if '"row"' in line],[[1]])

 def test_boundaries(self):
  source="SELECT 'a;''b'; /* ;\n ; */ SELECT x; -- ;\nSELECT `a;b`;"
  parts=list(statements(io.StringIO(source)))
  self.assertEqual(len(parts),3)
  self.assertIn("'a;''b'",parts[0]);self.assertIn('/* ;',parts[1]);self.assertIn('`a;b`',parts[2])
if __name__=='__main__':unittest.main()
