#!/usr/bin/env python3
"""Independent group-cache oracle, scan elimination, view isolation and budgets."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import copy,json,math,os
from minidb.engine import Database
from minidb.sql import DBError
from minidb import filters
db=Database('/data');checks=0
def q(sql):
    r=db.execute(sql)
    if not hasattr(r,'columns'):return r
    if r.path is None:return [json.loads(row) for row in r.encoded_rows]
    try:return [json.loads(row) for row in r.path.read_text().splitlines()]
    finally:r.path.unlink()
q('CREATE TABLE t(id BIGINT PRIMARY KEY,tag VARCHAR(16),n BIGINT,v VECTOR(128))')
tags=['a','é','中文','🙂','𐀀']
state={i-2500:[tags[i%5],i%10,[float(i%17)]*128] for i in range(5000)}
for start in range(-2500,2500,100):
    q('INSERT INTO t VALUES '+','.join(f"({i},'{state[i][0]}',{state[i][1]},'{json.dumps(state[i][2])}')" for i in range(start,start+100)))
q('CREATE VECTOR INDEX ix ON t(v)')
original=db.store.records;scans=[]
def records(*a,**kw):
    scans.append(1);yield from original(*a,**kw)
db.store.records=records
def check(where,predicate,value=0):
    global checks
    truth=sorted((math.fsum((x-value)**2 for x in row[2]),i) for i,row in state.items() if predicate(row))[:10]
    got=q("SELECT id,L2_DISTANCE(v,'"+json.dumps([float(value)]*128)+"') AS d FROM t WHERE "+where+' ORDER BY d LIMIT 10')
    assert got==[[i,math.sqrt(d)] for d,i in truth],where
    assert db.value_groups.bytes<=filters.GROUP_BYTES
    checks+=1
check("tag='a'",lambda r:r[0]=='a')
assert scans and not db.value_groups.entries
scans.clear();check("tag='中文'",lambda r:r[0]=='中文',1)
assert scans and any(groups is not None for groups,cost in db.value_groups.entries.values())
scans.clear();check("tag='é'",lambda r:r[0]=='é',1);assert not scans
check("tag='absent'",lambda r:False);assert not scans
check("tag>'🙂'",lambda r:r[0]>'🙂');assert not scans
check("tag>='é' AND tag<'🙂'",lambda r:'é'<=r[0]<'🙂');assert not scans
check('n=1',lambda r:r[1]==1)
scans.clear();check('n>=2 AND n<5',lambda r:2<=r[1]<5,1);assert scans
scans.clear();check('n=6',lambda r:r[1]==6);assert not scans
# Too many matching IDs use the existing streaming path.
scans.clear();check("tag>=''",lambda r:True);assert scans
check("tag='a' AND n>=5",lambda r:r[0]=='a' and r[1]>=5)
os.environ['MINIDB_NATIVE']='0';check("tag='𐀀'",lambda r:r[0]=='𐀀');os.environ.pop('MINIDB_NATIVE')
before=copy.deepcopy(state)
q('BEGIN');q("UPDATE t SET tag='🙂',n=9 WHERE id=-2500");state[-2500][:2]=['🙂',9]
check("tag='a'",lambda r:r[0]=='a');check("tag='🙂'",lambda r:r[0]=='🙂')
try:q("INSERT INTO t VALUES(-2500,'a',0,'"+json.dumps([0.]*128)+"')")
except DBError as e:assert e.code=='duplicate_key'
else:assert False
check('n=9',lambda r:r[1]==9)
q('ROLLBACK');state=before;check("tag='a'",lambda r:r[0]=='a')
q('DELETE FROM t WHERE id=-2499');state.pop(-2499)
check("tag='é'",lambda r:r[0]=='é');check("tag='中文'",lambda r:r[0]=='中文')
q('CREATE VECTOR INDEX ix2 ON t(v)');check("tag='🙂'",lambda r:r[0]=='🙂')
# High cardinality overflows the builder, storing only a negative decision.
q('CREATE TABLE u(id BIGINT PRIMARY KEY,n BIGINT,v VECTOR(16))')
for start in range(0,2000,100):
    q('INSERT INTO u VALUES '+','.join(f"({i},{i},'{json.dumps([float(i)]*16)}')" for i in range(start,start+100)))
q('CREATE VECTOR INDEX ix ON u(v)')
for n in (1,2):
    assert q("SELECT id FROM u WHERE n="+str(n)+" ORDER BY L2_DISTANCE(v,'"+json.dumps([0.]*16)+"') LIMIT 10")==[[n]]
assert any(groups is None for groups,cost in db.value_groups.entries.values())
assert db.value_groups.bytes<=filters.GROUP_BYTES
# Actual preflight under a reduced budget, plus LRU eviction of multiple views.
saved=filters.GROUP_BYTES;filters.GROUP_BYTES=4096
db.value_groups.entries.clear();db.value_groups.bytes=0
db.filter_cache.clear();db.filter_cache_bytes=0;db.filter_masks.clear();db.filter_masks_bytes=0
check("tag='a'",lambda r:r[0]=='a');check("tag='🙂'",lambda r:r[0]=='🙂')
assert db.value_groups.entries and all(g is None for g,c in db.value_groups.entries.values())
filters.GROUP_BYTES=saved
for i in range(30):
    builder=filters.GroupBuilder(256,saved)
    for j in range(5000):builder.add(j,j%2)
    db.value_groups.remember(('eviction',i),builder)
assert db.value_groups.bytes<=saved and len(db.value_groups.entries)<30
check("tag='中文'",lambda r:r[0]=='中文')
db.close();db=Database('/data');assert not db.value_groups.entries
check("tag='a'",lambda r:r[0]=='a');check("tag='中文'",lambda r:r[0]=='中文');db.close()
print(json.dumps({'status':'PASS','oracle_queries':checks,'rows':5000,'dimensions':128,'cache_budget':saved,'coverage':['new predicate without row scan','missing value','Unicode codepoint ranges','same-column AND','numeric comparisons','dense match fallback','multiple-column fallback','native disabled','transaction mutation','failed statement','rollback','committed delete','second index','high cardinality overflow','preflight budget fallback','LRU eviction','restart rebuild on demand']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=24m','--entrypoint','python3','minivectordb:groups-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/value-groups';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
