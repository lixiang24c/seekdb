import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from minivectordb.core import Database, DBError
from minivectordb.sql import SQL


class WalDeltaTests(unittest.TestCase):
    def test_delta_identical_vectors_keep_primary_key_ties(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',131]},'id')
            vector=[(i%13-6)/7 for i in range(131)]
            db.insert('t',({'id':i,'v':vector} for i in range(100)))
            db.create_vector_index('ix','t','v')
            db.modify('t',lambda r:r['id']==0,{'v':vector})
            self.assertEqual([r['id'] for r,d in db.topk('t','v',[.31]*131,3)],[0,1,2])
            db.close()

    def test_complete_commits_survive_a_partial_log_tail(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed(root)
            child='''
import os,sys
from minivectordb.core import Database
db=Database(sys.argv[1]);db.insert('a',[{'id':7,'v':[1,0]}]);os._exit(0)
'''
            subprocess.run([sys.executable,'-c',child,root],check=True)
            with (Path(root)/'database.wal').open('ab') as out:out.write(b'MVW1\x01\xff')
            db=Database(root);self.assertEqual([r['id'] for r in db.rows('a')],[7]);db.close()
            self.assertEqual((Path(root)/'database.wal').stat().st_size,0)

    def test_indexed_delta_keeps_base_files_and_filters_current_versions(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);sql=SQL(db)
            sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY,tag BIGINT,v VECTOR(2))')
            db.insert('t',({'id':i,'tag':0,'v':[i,0]} for i in range(512)))
            sql.execute('CREATE VECTOR INDEX ix ON t(v)')
            old_file=db.catalog['t']['file'];old_index=db.catalog['t']['index']['file']
            sql.execute('BEGIN');sql.execute('UPDATE t SET tag=1,v=[0,0] WHERE id=511')
            sql.execute('DELETE FROM t WHERE id=0')
            self.assertEqual(db.catalog['t']['file'],old_file)
            self.assertEqual(db.catalog['t']['index']['file'],old_index)
            query='SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2'
            self.assertEqual(list(sql.execute(query)['rows']),[[511,0.],[1,1.]])
            self.assertEqual(list(sql.execute('SELECT id FROM t WHERE tag=1 ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2')['rows']),[[511]])
            sql.execute('ROLLBACK');self.assertEqual(list(sql.execute(query)['rows']),[[0,0.],[1,1.]])
            sql.execute('UPDATE t SET tag=1,v=[0,0] WHERE id=511');sql.execute('DELETE FROM t WHERE id=0')
            db.close();db=Database(root);sql=SQL(db)
            self.assertEqual(list(sql.execute(query)['rows']),[[511,0.],[1,1.]])
            self.assertEqual(len(list(db.rows('t'))),511);db.close()

    def seed(self,root):
        db=Database(root)
        for name in ('a','b'):db.create(name,{'id':['BIGINT'],'v':['VECTOR',2]},'id')
        db.close()

    def test_one_durable_sync_commits_both_tables(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed(root);db=Database(root);db.begin()
            with patch('os.fsync',side_effect=AssertionError('per-file sync')),patch('os.fdatasync',wraps=os.fdatasync) as sync:
                db.insert('a',[{'id':1,'v':[1,2]}]);db.insert('b',[{'id':2,'v':[2,3]}]);db.commit()
                self.assertEqual(sync.call_count,1)
            db.close()

    def test_process_kills_never_publish_part_of_multitable_commit(self):
        child='''
import os,sys
from minivectordb.core import Database
from minivectordb.wal import RedoLog
db=Database(sys.argv[1]);db.begin()
db.insert('a',[{'id':1,'v':[1,2]}]);db.insert('b',[{'id':2,'v':[2,3]}])
stage=sys.argv[2];frame=RedoLog._frame;sync=os.fdatasync
def emit(out,kind,payload):
 if stage=='commit_before' and kind==RedoLog.COMMIT:os._exit(71)
 frame(out,kind,payload)
 if stage=='file' and kind==RedoLog.FILE:out.flush();os._exit(70)
 if stage=='commit_after' and kind==RedoLog.COMMIT:out.flush();os._exit(72)
def durable(fd):
 sync(fd)
 if stage=='synced':os._exit(73)
RedoLog._frame=staticmethod(emit);os.fdatasync=durable
db.commit();os._exit(0)
'''
        for stage in ('file','commit_before','commit_after','synced'):
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as root:
                self.seed(root)
                result=subprocess.run([sys.executable,'-c',child,root,stage])
                self.assertIn(result.returncode,(70,71,72,73))
                for _ in range(2):
                    db=Database(root);counts=[len(list(db.rows(name))) for name in ('a','b')]
                    self.assertEqual(counts,[0,0] if stage in ('file','commit_before') else [1,1]);db.close()

    def test_redo_restores_files_and_survives_interrupted_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed(root)
            child='''
import os,sys,json
from minivectordb.core import Database
db=Database(sys.argv[1]);db.begin()
db.insert('a',[{'id':i,'v':[i/8,0]} for i in range(100)])
db.insert('b',[{'id':8,'v':[0,8]}]);db.commit()
print(json.dumps(sorted(db._catalog_files(db.committed))),flush=True)
os._exit(0)
'''
            prior={p.name for p in Path(root).iterdir()}
            result=subprocess.run([sys.executable,'-c',child,root],text=True,capture_output=True,check=True)
            for name in set(json.loads(result.stdout))-prior:(Path(root)/name).unlink()
            interrupt='''
import os,sys
from pathlib import Path
from minivectordb.core import Database
opening=Path.open
def stop(path,*args,**kwargs):
 result=opening(path,*args,**kwargs)
 if args and args[0]=='wb' and path.name.startswith('table-'):os._exit(88)
 return result
Path.open=stop
Database(sys.argv[1])
'''
            self.assertEqual(subprocess.run([sys.executable,'-c',interrupt,root]).returncode,88)
            db=Database(root);self.assertEqual([r['id'] for r in db.rows('a')],list(range(100)))
            self.assertEqual(list(db.rows('b')),[{'id':8,'v':[0.,8.]}]);db.close()

    def test_uncertain_log_sync_requires_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            self.seed(root);db=Database(root);db.begin();db.insert('a',[{'id':1,'v':[1,0]}])
            with patch('os.fdatasync',side_effect=OSError('uncertain WAL sync')):
                with self.assertRaises(OSError):db.commit()
            with self.assertRaises(DBError):list(db.rows('a'))
            db.close();db=Database(root)
            self.assertEqual([r['id'] for r in db.rows('a')],[1]);db.close()

    def test_delta_delete_reinsert_commit_and_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',2]},'id')
            db.insert('t',({'id':i,'v':[i,0]} for i in range(512)))
            base=db.catalog['t']['segments'][0]
            db.begin();db.modify('t',lambda r:r['id']==511,{'v':[-1,0]})
            self.assertTrue(db.catalog['t']['changes']);self.assertIn(base,db.catalog['t']['segments'])
            self.assertEqual(db.topk('t','v',[-1,0],1)[0][0]['id'],511)
            db.modify('t',lambda r:r['id']==511)
            db.insert('t',[{'id':511,'v':[-2,0]}]);self.assertEqual(len(list(db.rows('t'))),512)
            db.rollback();self.assertEqual(next(r for r in db.rows('t') if r['id']==511)['v'],[511.,0.])
            for i in range(10):db.modify('t',lambda r:r['id']==i,{'v':[-i,0]})
            self.assertLess(len(db.catalog['t'].get('changes',[])),db.CHANGE_LIMIT)
            db.modify('t',lambda r:r['id']==100);db.close();db=Database(root)
            rows={r['id']:r['v'] for r in db.rows('t')}
            self.assertEqual(len(rows),511);self.assertNotIn(100,rows)
            for i in range(10):self.assertEqual(rows[i],[-float(i),0.])
            db.close()

    def test_failed_delta_statement_keeps_prior_transaction_changes(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT']},'id');db.insert('t',({'id':i} for i in range(100)))
            db.begin();db.modify('t',lambda r:r['id']==99)
            def broken(row):
                if row['id']==3:raise ValueError('predicate failure')
                return row['id']==2
            with self.assertRaises(ValueError):db.modify('t',broken)
            db.commit();db.close();db=Database(root)
            self.assertEqual([r['id'] for r in db.rows('t')],list(range(99)));db.close()


if __name__=='__main__':unittest.main()
