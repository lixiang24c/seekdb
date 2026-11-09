import subprocess,sys,tempfile,unittest
from minivectordb.core import Database
class RecoveryInterruptionTests(unittest.TestCase):
 def test_kill_during_cleanup_then_recover_again(self):
  with tempfile.TemporaryDirectory() as root:
   db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.insert('t',[{'id':1}]);db.close()
   seed="""
import os,sys
from minivectordb.core import Database
db=Database(sys.argv[1]);db.begin();db.insert('t',[{'id':2}]);os._exit(0)
"""
   subprocess.run([sys.executable,'-c',seed,root],check=True)
   recover="""
import os,sys
from pathlib import Path
from minivectordb.core import Database
original=Path.unlink
def interrupted(path,*args,**kwargs):
 original(path,*args,**kwargs);os._exit(88)
Path.unlink=interrupted
Database(sys.argv[1])
"""
   result=subprocess.run([sys.executable,'-c',recover,root]);self.assertEqual(result.returncode,88)
   db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1}]);db.close()
if __name__=='__main__':unittest.main()
