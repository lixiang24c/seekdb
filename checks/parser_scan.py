#!/usr/bin/env python3
"""Actual-image token/error equivalence to the prior character scanner."""
import json,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
REFERENCE=subprocess.check_output(['git','show','291d4a4a:minidb/sql.py'],cwd=ROOT,text=True)
PROBE='REFERENCE='+repr(REFERENCE)+'\n'+r"""
import itertools,json,random,sys,types
from minidb import sql
ref=types.ModuleType('reference_sql');sys.modules[ref.__name__]=ref
exec(compile(REFERENCE,'reference_sql.py','exec'),ref.__dict__)
def outcome(module,text):
    try:return ('tokens',[(t.kind,t.value) for t in module.tokenize(text)])
    except module.DBError as e:return ('error',e.code,str(e))
checks=0
def check(text):
    global checks
    assert outcome(sql,text)==outcome(ref,text),repr(text)
    checks+=1
for n in range(7):
    for chars in itertools.product("'`a\n",repeat=n):check(''.join(chars))
rng=random.Random(20261207)
alphabet="'`ab01+-/*;()= \n\r\\中文🙂"
for _ in range(1000):check(''.join(rng.choices(alphabet,k=rng.randrange(1,513))))
for value in ('',"'","''","a'b",'中文🙂\n\r\t','--/*;*/\\', 'x'*100000):
    check("INSERT INTO `a``b` VALUES('"+value.replace("'","''")+"'); -- tail")
    check('SELECT `'+value.replace('`','``')+'` FROM t')
for text in ("'unterminated",'`unterminated',"'''",'/* never ends',"'abc'@",'-- eof',"'--/*;*/'", "'a''''b'", "'a'''"):
    check(text)
print(json.dumps({'status':'PASS','differential_cases':checks,'reference_commit':'291d4a4a','coverage':['exhaustive short quote/backtick/newline strings','seeded malformed fragments','doubled quotes/backticks','empty/Unicode/long literals','comments and separators inside quotes','unterminated input','identical token kinds/values and error code/message']}))
"""
r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--entrypoint','python3','minivectordb:parser-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=30)
assert r.returncode==0,(r.stdout,r.stderr)
out=ROOT/'protocol-results/parser-scan';out.mkdir(parents=True,exist_ok=True)
(out/'result.json').write_text(r.stdout);print(r.stdout,end='')
