"""Kill actual child processes at manifest publication boundaries."""
import os,subprocess,sys,tempfile,unittest
from minivectordb.core import Database

CHILD='''
import os,sys
from minivectordb.core import Database
db=Database(sys.argv[1],wal=False)
real_replace=os.replace
real_sync=os.fsync
stage=sys.argv[2]
def replace(a,b):
 if stage=='before':os._exit(90)
 real_replace(a,b)
 if stage=='after':os._exit(91)
os.replace=replace
db.begin()
db.insert('t',[{'id':2},{'id':3}])
db.commit()
os._exit(0)
'''
class CrashBoundaryTests(unittest.TestCase):
 def test_manifest_atomicity_and_repeated_reopen(self):
  for stage in ('before','after'):
   with self.subTest(stage=stage),tempfile.TemporaryDirectory() as root:
    db=Database(root,wal=False);db.create('t',{'id':['BIGINT']},'id');db.insert('t',[{'id':1}]);db.close()
    result=subprocess.run([sys.executable,'-c',CHILD,root,stage])
    self.assertIn(result.returncode,(90,91))
    for repeat in range(3):
     recovered=Database(root,wal=False)
     ids=[r['id'] for r in recovered.rows('t')]
     self.assertIn(ids,([1],[1,2,3]))
     self.assertEqual(ids,[1] if stage=='before' else [1,2,3])
     recovered.close()
if __name__=='__main__':unittest.main()
