"""Local wall-clock/RSS diagnostic; does not reproduce official cgroup scoring."""
import argparse,json,random,resource,tempfile,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from minivectordb.core import Database
p=argparse.ArgumentParser();p.add_argument('--rows',type=int,default=1000);p.add_argument('--queries',type=int,default=100);p.add_argument('--index',action='store_true');args=p.parse_args()
rng=random.Random(42)
with tempfile.TemporaryDirectory() as root:
 db=Database(root);db.create('v',{'id':['BIGINT'],'tag':['BIGINT'],'v':['VECTOR',128]},'id')
 start=time.perf_counter()
 for offset in range(0,args.rows,100):
  db.insert('v',({'id':i,'tag':i%10,'v':[rng.random() for _ in range(128)]} for i in range(offset,min(offset+100,args.rows))))
 write=time.perf_counter()-start
 preparation=0
 if args.index:
  start=time.perf_counter();db.create_vector_index('ix','v','v');preparation=time.perf_counter()-start
 start=time.perf_counter()
 for i in range(args.queries):
  query=[rng.random() for _ in range(128)]
  found=db.topk('v','v',query,10,(lambda row:row['tag']==i%10) if i%2 else lambda row:True)
  assert len(found)==min(10,args.rows//10) if i%2 else len(found)==min(10,args.rows)
 query=time.perf_counter()-start
 print(json.dumps({'rows':args.rows,'queries':args.queries,'write_seconds':write,'query_seconds':query,'index_preparation_seconds':preparation,'indexed':args.index,'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'scope':'local diagnostics only; no official I/O or cgroup memory measurement'}))
