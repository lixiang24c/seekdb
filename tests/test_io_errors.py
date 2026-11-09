import tempfile,unittest
from unittest.mock import patch
from minivectordb.core import Database,DBError

class IOErrorTests(unittest.TestCase):
 def test_data_sync_failure_rolls_back_only_statement(self):
  with tempfile.TemporaryDirectory() as root:
   db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.begin();db.insert('t',[{'id':1}])
   with patch('minivectordb.core.os.fsync',side_effect=OSError('injected sync failure')):
    with self.assertRaises(OSError):db.insert('t',[{'id':2}])
   self.assertEqual(list(db.rows('t')),[{'id':1}]);db.commit();db.close()
   db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1}]);db.close()
 def test_commit_publication_uncertainty_requires_reopen(self):
  with tempfile.TemporaryDirectory() as root:
   db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.begin();db.insert('t',[{'id':1}])
   import os
   original=os.replace
   def replace_then_fail(source,target):
    original(source,target);raise OSError('injected post-rename failure')
   with patch('minivectordb.core.os.replace',side_effect=replace_then_fail):
    with self.assertRaises(OSError):db.commit()
   with self.assertRaises(DBError):list(db.rows('t'))
   db.close();db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1}]);db.close()
if __name__=='__main__':unittest.main()
