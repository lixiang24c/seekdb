import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from minivectordb.core import Database,DBError


class TieredSegmentTests(unittest.TestCase):
    def test_merge_levels_reopen_and_index_keep_all_rows(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.SEGMENT_FANOUT=4;db.SEGMENT_LIMIT=4
            db.create('t',{'id':['BIGINT'],'v':['VECTOR',1]},'id')
            for key in range(64):
                db.insert('t',[{'id':key,'v':[key/8]}])
                self.assertLessEqual(len(db.catalog['t']['segments']),4)
                self.assertEqual(sorted(r['id'] for r in db.rows('t')),list(range(key+1)))
            self.assertGreaterEqual(max(db.catalog['t']['segment_levels'].values()),2)
            db.close();db=Database(root)
            self.assertEqual({r['id']:r['v'] for r in db.rows('t')},{i:[i/8] for i in range(64)})
            db.create_vector_index('ix','t','v')
            self.assertEqual(db.catalog['t']['segment_levels'],{})
            self.assertEqual([r['id'] for r,d in db.topk('t','v',[0],3)],[0,1,2]);db.close()
    def test_failed_merge_and_rollback_preserve_prior_statements(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.SEGMENT_FANOUT=4;db.SEGMENT_LIMIT=4
            db.create('t',{'id':['BIGINT']},'id');db.insert('t',[{'id':0}]);db.begin()
            db.insert('t',[{'id':1}]);db.insert('t',[{'id':2}])
            with patch.object(db,'_indexed',side_effect=DBError('after merge fault')):
                with self.assertRaises(DBError):db.insert('t',[{'id':3}])
            self.assertEqual(sorted(r['id'] for r in db.rows('t')),[0,1,2])
            db.commit();db.begin();db.insert('t',[{'id':3}]);db.rollback()
            self.assertEqual(sorted(r['id'] for r in db.rows('t')),[0,1,2]);db.close()
            db=Database(root);self.assertEqual(sorted(r['id'] for r in db.rows('t')),[0,1,2]);db.close()
    def test_small_tables_do_not_trigger_early_merge(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT']},'id')
            for key in range(100):db.insert('t',[{'id':key}])
            self.assertEqual(len(db.catalog['t']['segments']),100)
            self.assertEqual(set(db.catalog['t']['segment_levels'].values()),{0})
            db.close()
            path=Path(root)/'manifest.json';legacy=json.loads(path.read_text());legacy['t'].pop('segment_levels');path.write_text(json.dumps(legacy))
            db=Database(root)
            for key in range(100,260):db.insert('t',[{'id':key}])
            self.assertEqual(sorted(r['id'] for r in db.rows('t')),list(range(260)))
            self.assertLess(len(db.catalog['t']['segments']),128);db.close()

if __name__=='__main__':unittest.main()
