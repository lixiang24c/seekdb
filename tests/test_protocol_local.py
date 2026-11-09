import io,unittest
from minivectordb.server import statements
class StatementTests(unittest.TestCase):
 def test_boundaries(self):
  source="SELECT 'a;''b'; /* ;\n ; */ SELECT x; -- ;\nSELECT `a;b`;"
  parts=list(statements(io.StringIO(source)))
  self.assertEqual(len(parts),3)
  self.assertIn("'a;''b'",parts[0]);self.assertIn('/* ;',parts[1]);self.assertIn('`a;b`',parts[2])
if __name__=='__main__':unittest.main()
