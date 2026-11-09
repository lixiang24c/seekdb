import tempfile
import unittest
from minivectordb.core import Database, DBError

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.tmp.name)
        self.db.create('t', {'id':['BIGINT'], 'text':['VARCHAR',4], 'v':['VECTOR',2]}, 'id')
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def row(self, i, v=None):
        return {'id':i, 'text':'中文🙂', 'v':v or [i,0]}
    def test_statement_atomicity(self):
        self.db.begin()
        self.db.insert('t', [self.row(1)])
        with self.assertRaises(DBError):
            self.db.insert('t', [self.row(2), self.row(1)])
        self.assertEqual([r['id'] for r in self.db.rows('t')], [1])
        self.db.commit()
        self.assertEqual(list(Database(self.tmp.name).rows('t')), [self.row(1)])
    def test_transaction_topk(self):
        self.db.insert('t', [self.row(1),self.row(2),self.row(3)])
        self.db.begin()
        self.db.modify('t', lambda r:r['id']==3, {'v':[0,0]})
        self.assertEqual(self.db.topk('t','v',[0,0],1)[0][0]['id'],3)
        self.db.modify('t',lambda r:r['id']==3)
        self.assertEqual(self.db.topk('t','v',[0,0],1)[0][0]['id'],1)
        self.db.rollback()
        self.assertEqual(len(list(self.db.rows('t'))),3)
    def test_filter_and_ties(self):
        self.db.insert('t', [self.row(3,[1,0]),self.row(2,[-1,0]),self.row(1,[0,0])])
        result=self.db.topk('t','v',[0,0],10,lambda r:r['id']>1)
        self.assertEqual([r['id'] for r,d in result],[2,3])
    def test_invalid_types(self):
        for vector in ([1], [float('nan'),0], [1e40,0]):
            with self.assertRaises(DBError): self.db.insert('t',[self.row(1,vector)])
        self.assertEqual(list(self.db.rows('t')),[])
    def test_abandoned_transaction_recovery(self):
        self.db.insert('t',[self.row(1)])
        self.db.begin()
        self.db.insert('t',[self.row(2)])
        # Simulates reopening after process loss, without close/rollback.
        restored=Database(self.tmp.name)
        self.assertEqual([r['id'] for r in restored.rows('t')],[1])
        self.db.pending=None

if __name__=='__main__': unittest.main()

class IndexTests(CoreTests):
    def test_index_updates_and_rollback(self):
        self.db.insert('t',[self.row(1),self.row(2)])
        self.db.create_vector_index('ix','t','v')
        self.db.begin()
        self.db.modify('t',lambda r:r['id']==2,{'v':[0,0]})
        self.assertEqual(self.db.topk('t','v',[0,0],1)[0][0]['id'],2)
        self.db.rollback()
        self.assertEqual(self.db.topk('t','v',[0,0],1)[0][0]['id'],1)
        self.db.modify('t',lambda r:r['id']==1)
        self.db.close()
        self.db=Database(self.tmp.name)
        self.assertEqual(self.db.topk('t','v',[0,0],1)[0][0]['id'],2)
