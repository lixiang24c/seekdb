import random,tempfile,unittest
from minivectordb.core import Database
class IndexTruthTests(unittest.TestCase):
 def test_exact_index_matches_independent_scan(self):
  rng=random.Random(931)
  with tempfile.TemporaryDirectory() as root:
   db=Database(root);db.create('t',{'id':['BIGINT'],'tag':['BIGINT'],'v':['VECTOR',8]},'id')
   db.insert('t',({'id':i,'tag':i%3,'v':[rng.randrange(-10,11)/4 for _ in range(8)]} for i in range(2100)))
   queries=[[rng.randrange(-10,11)/4 for _ in range(8)] for _ in range(8)]
   truth=[]
   for q in queries:
    for tag in (-1,0,2,7):
     rows=[r for r in db.rows('t') if tag==-1 or r['tag']==tag]
     rows.sort(key=lambda r:(sum((x-y)**2 for x,y in zip(r['v'],q)),r['id']))
     truth.append([r['id'] for r in rows[:10]])
   db.create_vector_index('ix','t','v')
   db.close();db=Database(root)
   actual=[]
   for q in queries:
    for tag in (-1,0,2,7):
     actual.append([r['id'] for r,d in db.topk('t','v',q,10,lambda r:tag==-1 or r['tag']==tag)])
   self.assertEqual(actual,truth)
   db.close()
if __name__=='__main__':unittest.main()
