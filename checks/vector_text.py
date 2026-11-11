#!/usr/bin/env python3
"""Bitwise/error equivalence for bounded whole-vector numeric validation."""
import json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=subprocess.check_output(['git','show','3c34679e:minidb/sql.py'],cwd=ROOT,text=True)
PROBE='REFERENCE='+repr(REFERENCE)+'\n'+r'''
import itertools,json,random,struct,sys,types
from minidb import sql
ref=types.ModuleType('reference_sql');sys.modules[ref.__name__]=ref
exec(compile(REFERENCE,'reference_sql.py','exec'),ref.__dict__)
checks=0
def result(module,text,dim):
    try:
        values=module.vector(text,dim)
        return ('bits',struct.pack('<'+str(len(values))+'f',*values))
    except module.DBError as e:return ('error',e.code,str(e))
def check(text,dim):
    global checks
    assert result(sql,text,dim)==result(ref,text,dim),(repr(text[:200]),dim)
    checks+=1
numbers=['0','-0','+0','00','01','+01','1.','.5','-.5','0e0','+1E-2','1e999','-1e999','١٢.٥','１２.５','3.4028234663852886e38','1e-150']
# Interpret escaped whitespace explicitly to include Python strip's control spaces.
spaces=['', ' ',chr(9),chr(10),chr(13),chr(28),chr(160),chr(8195)]
for number,space in itertools.product(numbers,spaces):
    for dim in (1,2,3):check('['+space+number+space+','+space+'-0'+space+']',dim)
for bad in ('NaN','Infinity','1_0','0x1','--1','++1','1e','1e+','1 2',"'1'",'true','null','[]','[0]',''):
    for position in range(3):
        parts=['0','1','2'];parts[position]=bad
        check('['+','.join(parts)+']',3);check('['+','.join(parts)+']',2)
for dim in (0,1,4095,4096,4097):check('['+','.join(['-0']*dim)+']',dim)
for size in (65535,65536,65537):check('[0'+' '*(size-3)+',1]',2)
for text in ('[]','[ ]','[0,]','[,0]','[1e999,bad]','[1e999]','0,1','[[1]]'):
    for dim in (0,1,2):check(text,dim)
rng=random.Random(20261208)
alphabet='0123456789+-.eE, abc\n'
for _ in range(1000):check('['+''.join(rng.choices(alphabet,k=rng.randrange(1,128)))+']',rng.randrange(4))
print(json.dumps({'status':'PASS','cases':checks,'reference_commit':'3c34679e','coverage':['bitwise float32 outcomes','negative zero','SQL signs/leading zeros/dot forms','Unicode decimal digits and whitespace','invalid number syntax','finite overflow','error precedence','4096-element fast-path boundary','64KiB-text boundary','empty vectors','seeded malformed literals']}))
'''
r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--entrypoint','python3','minivectordb:numeric-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=30)
assert r.returncode==0,(r.stdout,r.stderr)
out=ROOT/'protocol-results/vector-text';out.mkdir(parents=True,exist_ok=True)
(out/'result.json').write_text(r.stdout);print(r.stdout,end='')
