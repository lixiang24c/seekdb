import json,os,struct,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from minivectordb.core import Database,DBError

class CombinedSegmentTests(unittest.TestCase):
    def test_persist_combined_file_before_manifest_publication(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',1]},'id')
            events=[];sync=os.fsync;replace=os.replace
            def fsync(fd):events.append(('sync',os.readlink('/proc/self/fd/'+str(fd))));return sync(fd)
            def publish(a,b):events.append(('publish',str(b)));return replace(a,b)
            with patch('minivectordb.core.os.fsync',side_effect=fsync),patch('minivectordb.core.os.replace',side_effect=publish):
                db.insert('t',[{'id':9,'v':[1.]},{'id':-2,'v':[2.]}])
            table=db.catalog['t'];run=table['key_segments'][0]
            self.assertIn(run['file'],table['segments'])
            published=next(i for i,e in enumerate(events) if e[0]=='publish')
            self.assertIn(('sync',str(Path(root)/run['file'])),events[:published])
            self.assertIn(('sync',root),events[published+1:])
            with (Path(root)/run['file']).open('rb') as source:
                self.assertEqual(source.read(9),b'MVDBROW2\n');end=struct.unpack('<Q',source.read(8))[0]
                self.assertEqual(end,run['key_offset']);source.seek(end)
                self.assertEqual(struct.unpack('<2q',source.read()),(-2,9))
            db.close();db=Database(root);self.assertEqual(sorted(r['id'] for r in db.rows('t')),[-2,9]);db.close()
    def test_key_generation_failure_preserves_prior_transaction_statement(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.begin();db.insert('t',[{'id':-1}])
            with patch.object(Database,'_row_span',side_effect=DBError('injected footer staging failure')):
                with self.assertRaises(DBError):db.insert('t',({'id':i} for i in range(4200)))
            db.commit();self.assertEqual(list(db.rows('t')),[{'id':-1}])
            db.insert('t',[{'id':0}]);db.close()
    def test_crash_after_combined_sync_before_publication(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.insert('t',[{'id':1}]);db.close()
            child="""
import os,sys
from minivectordb.core import Database
db=Database(sys.argv[1])
def stop(*args):os._exit(79)
db._check_keys=stop
db.insert('t',[{'id':2}])
"""
            process=subprocess.run([sys.executable,'-c',child,root]);self.assertEqual(process.returncode,79)
            db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1}]);db.insert('t',[{'id':2}]);db.close()
    def test_legacy_binary_and_key_runs_upgrade_without_data_retention(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root);scalar=b'{"id":3}'
            (path/'table-old.rows').write_bytes(b'MVDBROW1\n'+struct.pack('<III',len(scalar),1,1)+scalar+b'v'+struct.pack('<f',3.))
            (path/'keys-base.bin').write_bytes(b'');(path/'keys-run.bin').write_bytes(struct.pack('<q',3))
            (path/'manifest.json').write_text(json.dumps({'t':{'columns':{'id':['BIGINT'],'v':['VECTOR',1]},'primary':'id','file':'table-old.rows','keys':'keys-base.bin','key_segments':['keys-run.bin']}}))
            db=Database(root);db.insert('t',[{'id':1,'v':[1.]}])
            with self.assertRaises(DBError):db.insert('t',[{'id':3,'v':[0.]}])
            db.create_vector_index('ix','t','v')
            self.assertEqual(db.catalog['t'].get('key_segments'),[])
            self.assertEqual(len(list(path.glob('table-*.rows'))),1)
            db.modify('t',lambda row:row['id']==3,{'v':[0.]});db.close();db=Database(root)
            self.assertEqual([r['id'] for r,d in db.topk('t','v',[0],10)],[3,1]);db.close()
if __name__=='__main__':unittest.main()
