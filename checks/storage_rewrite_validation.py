#!/usr/bin/env python3
"""Lossless rewrite, mixed extent truth and real SIGKILL boundaries.
Host mode injects a 16MiB *budget only*: it is not cgroup/OOM evidence.
"""
import argparse,array,copy,hashlib,json,math,random,shutil,struct,traceback
from pathlib import Path
from de2_query_validation import EvidenceFixture,diagnostics,q,verify
from native_validation import f32,identical
from native_journal import frame_list

DIM=128;N=30000

def state_for(shape='mixed',count=N):
 rng=random.Random(42);state={}
 for i in range(count):
  v=[f32(float(f'{rng.uniform(-1,1):.4f}')) for _ in range(DIM)]
  if shape=='random' or (shape=='mixed' and 100<=i<200):v=[f32(rng.gauss(0,1)) for _ in range(DIM)]
  if shape=='mixed':
   if i<100:v=[float((i+j)%23)/16 for j in range(DIM)]
   if 200<=i<300:v=[f32((40000+(i+j)%1100)*.0001) for j in range(DIM)]
   if 300<=i<400:v[:4]=[-0.,2**-149,-2**-149,f32(3.4028234663852886e38)]
  state[i]=[i%10,array.array('f',v),('中😀'+('x'*4096) if shape=='mixed' and i==200 else 'x'*16)]
 return state

def load(s,state):
 s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,body VARCHAR(8192),v VECTOR(128))')
 keys=list(state)
 for start in range(0,len(keys),100):
  q(s,'INSERT INTO t VALUES '+','.join(f"({i},{state[i][0]},'{state[i][2]}',"+json.dumps(list(state[i][1]))+')' for i in keys[start:start+100]))
 q(s,'CREATE VECTOR INDEX ix ON t(v)')

def codecs(s):
 # Only read host files in correctness/diagnostic runs, never timed runs.
 tables={}
 for _,_,m in frame_list(s.root/'queryable.log'):
  if m['kind']=='catalog':tables=m['tables']
  if m['kind']=='delta':
   for name,c in m['changes'].items():
    if c is None:tables.pop(name,None);continue
    t=tables.setdefault(name,{'extents':[]});t['schema']=c['schema'];ext=t['extents'][:c['length']]
    for i,e in c['patches']:
     if i==len(ext):ext.append(e)
     else:ext[i]=e
    t['extents']=ext
 return [e[9] if len(e)>9 else 0 for e in tables['t']['extents']]

def all_bits(s,state):
 digest=hashlib.sha256();keys=sorted(state)
 for start in range(0,len(keys),100):
  ids=keys[start:start+100]
  rows=q(s,f'SELECT id,g,body,v FROM t WHERE id>={ids[0]} AND id<={ids[-1]} ORDER BY id')['rows']
  assert [r[0] for r in rows]==ids
  for i,g,body,v in rows:
   expected=state[i];assert [g,body]==[expected[0],expected[2]]
   raw=struct.pack('<128f',*v);assert raw==struct.pack('<128f',*expected[1]),('float32 mismatch',i)
   digest.update(struct.pack('<q',i)+raw)
 return digest.hexdigest()

def query_truth(s,state):
 count=0
 for j in range(6):
  v=[f32((j-2)*.125)]*DIM
  for projection in ('id','distance','scalar'):
   verify(s,state,v,None if j%2==0 else j%10,10,projection=projection);count+=1
 # Indexed PK lookup and vector projection are independently compared.
 for i in (0,100,200,300,999,29999):
  if i in state:
   rows=q(s,f'SELECT v FROM t WHERE id={i}')['rows'];assert struct.pack('<128f',*rows[0][0])==struct.pack('<128f',*state[i][1])
 return count

def correctness(args):
 state=state_for();s=EvidenceFixture(args,'mixed');results={};query='SELECT id FROM t ORDER BY L2_DISTANCE(v,'+json.dumps([0.]*DIM)+') LIMIT 10'
 try:
  load(s,state);assert set(codecs(s))=={0};before=(s.root/'queryable.log').stat().st_size
  q(s,'BEGIN');q(s,query);q(s,'ROLLBACK');q(s,query[:-2]+'0');q(s,'SELECT id FROM t WHERE id=0');assert set(codecs(s))=={0}
  query_truth(s,state);cs=codecs(s);assert 0 in cs and 0x01000003 in cs and 0x01000402 in cs and 0x01000401 in cs,set(cs)
  results.update(codecs=sorted(set(cs)),raw_bytes=before,rewritten_bytes=(s.root/'queryable.log').stat().st_size,initial_float32_digest=all_bits(s,state));checks=18
  # Mixed raw appends and encoded existing extents, statement atomicity,
  # failed statement followed by further writes; both COMMIT and ROLLBACK.
  s.shutdown();results["rewrite_process_diagnostics"]=diagnostics(s);assert results["rewrite_process_diagnostics"]["PRESSURE_STORAGE_PATH"]["rewrites"]==1;s.start()
  saved=copy.deepcopy(state)
  for ending in ('ROLLBACK','COMMIT','KILL'):
   q(s,'BEGIN');q(s,'DELETE FROM t WHERE id=0');state.pop(0)
   z=[-0.]+[0.]*(DIM-1);q(s,"INSERT INTO t VALUES (0,9,'again',"+json.dumps(z)+')');state[0]=[9,array.array('f',z),'again']
   q(s,"UPDATE t SET g=9,body='updated',v="+json.dumps(z)+' WHERE id=100');state[100]=[9,array.array('f',z),'updated']
   assert not s.request("INSERT INTO t VALUES (40000,1,'atomic',"+json.dumps(z)+"),(0,1,'dup',"+json.dumps(z)+')')['ok']
   assert q(s,'SELECT id FROM t WHERE id=40000')['rows']==[]
   q(s,'DELETE FROM t WHERE id=2');state.pop(2,None);checks+=query_truth(s,state)
   if ending=='KILL':s.crash();s.start();state=copy.deepcopy(saved)
   else:
    q(s,ending)
    if ending=='ROLLBACK':state=copy.deepcopy(saved)
    else:saved=copy.deepcopy(state)
   checks+=query_truth(s,state);all_bits(s,state)
  # Reclamation of encoded extents with tombstones (ordinary checkpoint).
  old=(s.root/'queryable.log').stat().st_size
  q(s,'DELETE FROM t WHERE id>=2000');state={i:v for i,v in state.items() if i<2000}
  assert (s.root/'queryable.log').stat().st_size<old;checks+=query_truth(s,state);all_bits(s,state)
  s.crash();s.start();checks+=query_truth(s,state);results['recovered_float32_digest']=all_bits(s,state)
  s.shutdown();results.update(status='PASS',queries=checks,diagnostics=diagnostics(s))
  env=dict(x.split('=',1) for x in args.env)
  if env.get('MINIDB_SIMD')=='0':assert results['diagnostics']['MINIDB_PATH']['simd']==0
  if env.get('MINIDB_INDEX_MMAP')=='0':assert results['diagnostics']['MINIDB_PATH']['mapped_opens']==0
  elif results['diagnostics']['MINIDB_PATH']['simd']:
   assert results['diagnostics']['BLOCK_PATH']['certified']>0,results['diagnostics']
  return results
 finally:s.close()

def fallback(args):
 results={}
 for shape,enabled,budget,count in [('random',True,16,N),('decimal',False,16,N),('decimal',True,16,N*2)]:
  state=state_for(shape,count);local=copy.copy(args);local.env=list(args.env)+([] if enabled else ['MINIDB_PRESSURE_REPACK=0']);local.memory=f'{budget}m'
  if args.binary:local.env=[x for x in local.env if not x.startswith('MV_TEST_MEMORY_MB=')]+[f'MV_TEST_MEMORY_MB={budget}']
  s=EvidenceFixture(local,f'fallback-{shape}-{enabled}-{budget}-{count}')
  try:
   load(s,state);query_truth(s,state);assert set(codecs(s))=={0};s.shutdown();results[f'{shape}-{enabled}-{budget}-{count}']={'status':'PASS','diagnostics':diagnostics(s)}
   assert results[f'{shape}-{enabled}-{budget}-{count}']['diagnostics']['PRESSURE_STORAGE_PATH']['rewrites']==0
  finally:s.close()
 return results

def faults(args):
 state=state_for('decimal');seed=EvidenceFixture(args,'fault-seed');saved=args.output/'raw-seed'
 try:load(seed,state);seed.shutdown();shutil.copytree(seed.root,saved)
 finally:seed.close()
 results=[]
 for point in ('checkpoint-write','checkpoint-sync','checkpoint-sync-after','checkpoint-before-rename','checkpoint-after-rename','checkpoint-directory','checkpoint-directory-after'):
  s=EvidenceFixture(args,point)
  try:
   shutil.copytree(saved,s.root,dirs_exist_ok=True)
   # Copies must be writable to restricted container root, never chmod user's directories.
   for p in s.root.iterdir():p.chmod(0o666)
   s.start();s.arm(point);s.request('SELECT id FROM t ORDER BY L2_DISTANCE(v,'+json.dumps([0.]*DIM)+') LIMIT 10',wait=False);s.kill_at_marker(point)
   for _ in range(2):s.arm('recover-read');s.start(ready=False);s.kill_at_marker('recover-read')
   s.start();digest=all_bits(s,state);query_truth(s,state);s.shutdown();assert not (s.root/'queryable.next').exists()
   s.start();assert all_bits(s,state)==digest;s.shutdown()
   results.append({'point':point,'status':'PASS','recovery_interruptions':2,'all_rows_float32_sha256':digest,'ack_before_boundary':False})
  finally:s.close()
 shutil.rmtree(saved);return results

def main():
 p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--binary');g.add_argument('--image')
 p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--memory',default='16m');p.add_argument('--cpus',default='1');p.add_argument('--cgroupns',default=None);p.add_argument('--env',action='append',default=[]);p.add_argument('--section',choices=['correctness','fallback','faults','all'],default='all');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 a.env+=['MINIDB_POSTINGS=0']
 if a.binary:a.env+=['MV_TEST_MEMORY_MB=16']
 r={'status':'RUNNING','host_budget_simulation_only':bool(a.binary),'image':a.image,'checks':{}}
 try:
  for name,fn in [('correctness',correctness),('fallback',fallback),('faults',faults)]:
   if a.section in (name,'all'):r['checks'][name]=fn(a);print(name,'PASS',flush=True)
  r['status']='PASS'
 except BaseException as e:r.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
 finally:(a.output/'report.json').write_text(json.dumps(r,indent=2)+'\n')
if __name__=='__main__':main()
