#!/usr/bin/env python3
"""Independent truth for remaining 0f5 query changes and storage/cache combinations."""
import argparse,array,copy,json,math,random,struct,traceback
from pathlib import Path
from de2_query_validation import EvidenceFixture,diagnostics,q
from native_validation import f32,identical,literal
from second_query_validation import Truth as BaseTruth

class Truth:
 def __init__(self,state):
  self.state=state;self.ids=list(state)
  try:
   import numpy as np
   self.np=np;self.matrix=np.array([state[i][1] for i in self.ids],dtype=np.float64)
  except ImportError:self.np=None
 def ranked(self,v,conditions,k):
  def accept(i):
   row=self.state[i]
   for col,op,b in conditions:
    a={'id':i,'g':row[0],'body':row[2],'h':row[3]}[col]
    if not {'=':a==b,'>':a>b,'<':a<b,'>=':a>=b,'<=':a<=b}[op]:return False
   return True
  ix=[j for j,i in enumerate(self.ids) if accept(i)];candidates=ix
  if self.np is not None and len(ix)>k and k:
   np=self.np;sums=np.empty(len(ix));qq=np.array(v)
   for start in range(0,len(ix),256):
    delta=self.matrix[ix[start:start+256]]-qq;sums[start:start+256]=np.sum(delta*delta,axis=1)
   eps=8*len(v)*2**-52;lower=np.nextafter(sums/(1+eps),-np.inf);upper=np.nextafter(sums/(1-eps),np.inf);cutoff=float(np.partition(upper,k-1)[k-1]);candidates=[ix[j] for j in np.flatnonzero(lower<=cutoff)]
  rank=sorted((math.fsum((x-y)*(x-y) for x,y in zip(self.state[self.ids[j]][1],v)),self.ids[j]) for j in candidates)[:k]
  if self.np is not None and len(ix)>k and k:
   omitted=lower[lower>cutoff];assert not len(omitted) or float(omitted.min())>rank[-1][0]
  return rank

def raw(v):return '['+','.join(repr(float(x)) for x in v)+']'
def sqlvalue(v):return literal(v) if isinstance(v,str) else str(v)
def check(s,truth,v,conditions=(),fields=('id',),k=10,quoted=False,other=None):
 expr='L2_DISTANCE(v,'+(literal(raw(v)) if quoted else raw(v))+')';different='L2_DISTANCE(v,'+raw(other or v)+')'
 cols=[expr+' AS d' if f=='d' else different+' AS other' if f=='other' else f for f in fields]
 sql='SELECT '+','.join(cols)+' FROM t '+('WHERE '+' AND '.join(c+op+sqlvalue(b) for c,op,b in conditions)+' ' if conditions else '')+'ORDER BY '+expr+f' LIMIT {k}'
 rank=truth.ranked(v,conditions,k);want=[]
 for score,i in rank:
  row=truth.state[i];values={'id':i,'g':row[0],'body':row[2],'h':row[3],'v':list(row[1]),'d':math.sqrt(score)}
  if 'other' in fields:values['other']=math.sqrt(math.fsum((x-y)*(x-y) for x,y in zip(row[1],other or v)))
  want.append([values[f] for f in fields])
 got=q(s,sql)['rows']
 try:identical(got,want)
 except BaseException:
  (s.args.output/'minimal-query.json').write_text(json.dumps({'sql':sql,'actual':got,'expected':want},ensure_ascii=False));raise
 return 1

TEXTS=["a'b",'中😀','x\\y','','] bracket [','plain','x"y','line\nnext']
def state_for(n,dim,base=0,holes=False):
 rng=random.Random(42);out={}
 for j in range(n):
  v=[f32(float(f'{rng.uniform(-1,1):.4f}')) for _ in range(dim)];out[base+j*(2 if holes else 1)]=[j%11,array.array('f',v),TEXTS[j%len(TEXTS)],j%7]
 return out

def load(s,state,dim):
 s.start();q(s,f'CREATE TABLE t(id BIGINT PRIMARY KEY,g BIGINT,body VARCHAR(8192),v VECTOR({dim}),h BIGINT)')
 keys=list(state)
 for start in range(0,len(keys),50):q(s,'INSERT INTO t VALUES '+','.join(f'({i},{state[i][0]},'+literal(state[i][2])+','+raw(state[i][1])+f',{state[i][3]})' for i in keys[start:start+50]))
 q(s,'CREATE VECTOR INDEX ix ON t(v)')

def mutate_cycle(s,state,cycle):
 keys=sorted(state);first,second=keys[0],keys[1];dim=len(state[first][1]);z=[0.]*dim;saved=copy.deepcopy(state);count=0
 for ending in ('ROLLBACK','COMMIT','KILL'):
  q(s,'BEGIN');q(s,'DELETE FROM t WHERE id='+str(first));q(s,f'INSERT INTO t VALUES ({first},4,'+literal("a'b")+','+raw(z)+',5)');state[first]=[4,array.array('f',z),"a'b",5]
  q(s,'UPDATE t SET body='+literal('中😀')+',g=4,h=5,v='+raw(z)+' WHERE id='+str(second));state[second]=[4,array.array('f',z),'中😀',5]
  third=keys[2];assert not s.request('INSERT INTO t VALUES '+f'({third},1,\'dup\','+raw(z)+',1)')['ok']
  q(s,f'UPDATE t SET h=6 WHERE id={second}');state[second][3]=6;count+=cycle(Truth(state))
  if ending=='KILL':s.crash();s.start();state.clear();state.update(copy.deepcopy(saved))
  else:
   q(s,ending)
   if ending=='ROLLBACK':state.clear();state.update(copy.deepcopy(saved))
   else:saved=copy.deepcopy(state)
  count+=cycle(Truth(state))
 s.shutdown();s.start();count+=cycle(Truth(state));return count

def text_projection(args):
 records={};total=0
 for label,base,holes in [('negative',-2000,True),('minimum',-(1<<63),True),('maximum',(1<<63)-2400,True)]:
  state=state_for(1153,128,base,holes);s=EvidenceFixture(args,'text-'+label);count=0
  try:
   if label=='negative':
    keys=list(state)
    for i,g,h in [(keys[0],-(1<<63),(1<<63)-1),(keys[1],(1<<63)-1,-(1<<63))]:state[i][0]=g;state[i][3]=h;state[i][1]=array.array('f',[0.]*128)
   load(s,state,128)
   def cycle(truth):
    n=0
    for j in range(16):
     v=[f32((j-8)*.03125)]*128
     conditions=[('body','=',TEXTS[j%8])]
     n+=check(s,truth,v,conditions,fields=('id','body','g','h','body','d'),quoted=True)
     n+=check(s,truth,v,[('body','>=',TEXTS[j%8]),('g','<=',j%11),('h','>=',j%7)],fields=('id','d'))
    for _ in range(4):n+=check(s,truth,[.125]*128,[('body','>=',"a'b"),('g','>=',2),('h','<=',5)],fields=('id','body','g'))
    n+=check(s,truth,[0.]*128,[('body','=',"a'b")],fields=('id','other'),other=[.25]*128)
    n+=check(s,truth,[0.]*128,[('body','=',"a'b"),('g','=',999)],fields=('id','body','d'))
    return n
   count+=cycle(Truth(state))
   # A populated string template must not turn invalid SQL or a malformed
   # vector into a success response using its previous bindings.
   suffix=' ORDER BY L2_DISTANCE(v,'+raw([0.]*128)+') LIMIT 10'
   for bad in ["SELECT id FROM t WHERE body='a''b' OR g=1"+suffix,
               "SELECT id FROM t WHERE body='unterminated"+suffix,
               "SELECT id FROM t WHERE body=NULL"+suffix,
               "SELECT id FROM t WHERE body='a''b' ORDER BY L2_DISTANCE(v,[0]) LIMIT 10"]:
    result=s.request(bad);assert not result['ok'],(bad,result);count+=1
   count+=check(s,Truth(state),[0.]*128,[('body','=',"a'b")],fields=('id','body','g','d'))
   if label=='negative':count+=mutate_cycle(s,state,cycle)
   # Very wide PK span rejects dictionary addressing, preserving projection.
   if label=='minimum':
    key=(1<<63)-1;v=[0.]*128;q(s,f"INSERT INTO t VALUES ({key},-9223372036854775808,'edge',"+raw(v)+',9223372036854775807)');state[key]=[-(1<<63),array.array('f',v),'edge',(1<<63)-1];count+=cycle(Truth(state));count+=check(s,Truth(state),v,fields=('id','g','h','body'))
   s.shutdown();d=diagnostics(s)
   env=dict(x.split('=',1) for x in args.env)
   if env.get('MINIDB_TEXT_BLOCKS')=='0':assert d['TEXT_FILTER_PATH']['scans']==0,d
   if env.get('MINIDB_BLOCK_FILTERS')=='0':assert d['BLOCK_FILTER_PATH']['hits']==0,d
   if env.get('MINIDB_COLUMN_PROJECTION')=='0':assert d['COLUMN_PROJECTION_PATH']['hits']==0,d
   if env.get('MINIDB_TEXT_PLANS')=='0':assert d['MINIDB_PATH']['plan_hits']==0,d
   if env.get('MINIDB_INDEX_MMAP')=='0':assert d['MINIDB_PATH']['mapped_opens']==0 and d['BLOCK_PATH']['scans']==0,d
   if env.get('MINIDB_SIMD')=='0':assert d['MINIDB_PATH']['simd']==0,d
   records[label]={'queries':count,'diagnostics':d};total+=count
   if args.require_paths and label in ('negative','maximum'):
    assert d['TEXT_FILTER_PATH']['scans']>0 and d['COLUMN_PROJECTION_PATH']['hits']>0 and d['BLOCK_FILTER_PATH']['hits']>0,d
  finally:s.close()
 return {'queries':total,'cases':records}

def combined(args):
 local=copy.copy(args);local.env=list(args.env)
 if args.binary:local.env+=['MV_TEST_MEMORY_MB=16']
 local.memory='16m';state=state_for(30000,128);state[29999]=[4,array.array('f',[0.]*128),"a'b",5];s=EvidenceFixture(local,'compressed-partial');count=0
 try:
  load(s,state,128)
  def cycle(truth):
   n=0
   for j in range(12):
    v=[f32((j%3)*.125)]*128
    n+=check(s,truth,v,[('body','=',"a'b")],fields=('id','d'),quoted=True)
    n+=check(s,truth,v,[('body','=',"a'b"),('g','>=',3),('h','<=',5)],fields=('id','body','g','d'),quoted=True)
   return n
  count+=cycle(Truth(state));s.shutdown();initial=diagnostics(s);s.start()
  count+=mutate_cycle(s,state,cycle);s.shutdown();d=diagnostics(s)
  assert initial['PRESSURE_STORAGE_PATH']['rewrites']==1,initial
  if args.require_paths:
   assert 0<initial['BLOCK_PATH']['slots']<len(state) and initial['BLOCK_PATH']['scans']>0 and initial['BLOCK_PATH']['certified']==0,initial
   assert initial['TEXT_FILTER_PATH']['scans']>0,initial
  return {'queries':count,'initial_diagnostics':initial,'final_diagnostics':d,'tail_winner':29999,'raw_and_encoded_after_dml':True}
 finally:s.close()

def wide(args):
 records={};rng=random.Random(43);total=0
 for dim in map(int,args.dimensions.split(',')):
  state=state_for(1153,dim);s=EvidenceFixture(args,'wide-'+str(dim));count=0
  try:
   load(s,state,dim);truth=Truth(state)
   for j in range(8):
    v=[f32(rng.uniform(-1,1)) for _ in range(dim)]
    count+=check(s,truth,v,[] if j%2==0 else [('g','>=',4)],fields=('id',))
   s.shutdown();fast=diagnostics(s);s.start()
   # Extreme norms should safely decline the cache screen. Keep that fallback
   # separate from the normal-data path-hit requirement.
   q(s,'BEGIN')
   for i in range(32):
    v=[f32(1+(i%3)*2**-23)]*dim;q(s,'UPDATE t SET v='+raw(v)+f' WHERE id={i}');state[i][1]=array.array('f',v)
   for i,x in [(32,2**-149),(33,-2**-149),(34,f32(3.4028234663852886e38))]:
    v=[x]*dim;q(s,'UPDATE t SET v='+raw(v)+f' WHERE id={i}');state[i][1]=array.array('f',v)
   q(s,'COMMIT');truth=Truth(state)
   for j in range(10):
    v=[f32(rng.uniform(-1,1)) for _ in range(dim)] if j<5 else [0. if j%2 else 1.]*dim
    if dim>512:v[511]=1.;v[512]=-1.
    conditions=[] if j%2==0 else [('g','>=',4)]
    for fields in [('id',),('id','d')]:count+=check(s,truth,v,conditions,fields=fields)
   if dim in (528,4096):
    def cycle(t):return check(s,t,[0.]*dim,fields=('id','d'))+check(s,t,[0.]*dim,[('g','=',4)],fields=('id','body'))
    count+=mutate_cycle(s,state,cycle)
   s.shutdown();d=diagnostics(s);records[str(dim)]={'queries':count,'normal_diagnostics':fast,'diagnostics':d};total+=count
   if args.require_paths and dim!=513:assert fast['DYNAMIC_BLOCK_PATH']['scans']>0,fast
   if dim==513:assert d['DYNAMIC_BLOCK_PATH']['scans']==0,d
  finally:s.close()
 return {'queries':total,'dimensions':records}

def wide_compact(args):
 local=copy.copy(args);local.env=list(args.env);local.memory='16m'
 if args.binary:local.env+=['MV_TEST_MEMORY_MB=16']
 dim=528;state=state_for(8000,dim);state[7999]=[4,array.array('f',[0.]*dim),"a'b",5];s=EvidenceFixture(local,'wide-compressed');count=0
 try:
  load(s,state,dim)
  def cycle():
   nonlocal count
   truth=Truth(state)
   for j in range(8):count+=check(s,truth,[f32(j*.03125)]*dim,[] if j%2==0 else [('g','>=',4)],fields=('id',) if j%3 else ('id','d'))
  cycle();s.shutdown();first=diagnostics(s);assert first['PRESSURE_STORAGE_PATH']['rewrites']==1,first;s.start()
  if args.require_paths:assert 0<first['BLOCK_PATH']['slots']<8000 and first['BLOCK_PATH']['certified']==0 and first['DYNAMIC_BLOCK_PATH']['scans']>0,first
  q(s,'DELETE FROM t WHERE id>=2000');state={i:r for i,r in state.items() if i<2000};cycle();s.shutdown();s.start();cycle();s.shutdown();last=diagnostics(s)
  if args.require_paths:assert last['DYNAMIC_BLOCK_PATH']['scans']>0 and last['BLOCK_PATH']['certified']==0,last
  return {'queries':count,'initial_diagnostics':first,'full_compressed_diagnostics':last,'raw_offset_certification_disabled':True}
 finally:s.close()

def main():
 p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--binary');g.add_argument('--image');p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cpus',default='1');p.add_argument('--memory',default='384m');p.add_argument('--cgroupns',default=None);p.add_argument('--env',action='append',default=[]);p.add_argument('--section',choices=['text_projection','combined','wide','wide_compact','all'],default='all');p.add_argument('--require-paths',action='store_true');p.add_argument('--dimensions',default='16,496,512,528,768,1024,4080,4096,513');a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False);a.env+=['MINIDB_POSTINGS=0'];r={'status':'RUNNING','host_only':bool(a.binary),'checks':{}}
 try:
  for name,fn in [('text_projection',text_projection),('combined',combined),('wide',wide),('wide_compact',wide_compact)]:
   if a.section in (name,'all'):r['checks'][name]=fn(a);print(name,'PASS',flush=True)
  r['status']='PASS'
 except BaseException as e:r.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
 finally:(a.output/'report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
if __name__=='__main__':main()
