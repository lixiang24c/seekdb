import random,tempfile,unittest
from minivectordb.core import Database,DBError

class ModelTests(unittest.TestCase):
 def test_random_transactions_against_independent_model(self):
  rng=random.Random(813)
  with tempfile.TemporaryDirectory() as root:
   db=Database(root);db.create('t',{'id':['BIGINT'],'v':['VECTOR',2]},'id')
   committed={};pending=None
   for step in range(200):
    state=committed if pending is None else pending
    action=rng.choice(['insert','delete','update','query','begin','finish','restart'])
    key=rng.randrange(20)
    if action=='insert':
     row={'id':key,'v':[float(rng.randrange(-3,4)),0.]}
     if key in state:
      with self.assertRaises(DBError):db.insert('t',[row])
     else:db.insert('t',[row]);state[key]=row
    elif action=='delete':
     db.modify('t',lambda r:r['id']==key);state.pop(key,None)
    elif action=='update':
     vector=[float(rng.randrange(-3,4)),0.]
     db.modify('t',lambda r:r['id']==key,{'v':vector})
     if key in state:state[key]=dict(state[key],v=vector)
    elif action=='begin' and pending is None:
     db.begin();pending=dict(committed)
    elif action=='finish' and pending is not None:
     if rng.randrange(2):db.commit();committed=pending
     else:db.rollback()
     pending=None
    elif action=='restart':
     db.close();db=Database(root);pending=None
    state=committed if pending is None else pending
    self.assertEqual({r['id']:r for r in db.rows('t')},state)
    expected=sorted((r for r in state.values() if r['id']%2),key=lambda r:(r['v'][0]**2,r['id']))[:5]
    actual=[r for r,d in db.topk('t','v',[0,0],5,lambda r:r['id']%2)]
    self.assertEqual(actual,expected)
   db.close()
