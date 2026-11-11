#!/usr/bin/env python3
"""Bitwise comparison to scalar IEEE float32 conversion, including rejection edges."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import json,math,random,struct
from minidb.sql import vector,DBError
rng=random.Random(20261206)
maximum=struct.unpack('<f',bytes.fromhex('ffff7f7f'))[0]
pool=[0.,-0.,2.**-150,math.nextafter(2.**-150,math.inf),2.**-149,-2.**-149,2.**-126,1.,-1.,math.nextafter(1.,math.inf),maximum,-maximum,math.nextafter(maximum,0.),math.nextafter(maximum,math.inf)]
checks=0
for dim in (1,2,3,16,31,32,127,128,129,512,4096):
    for distribution in range(3):
        values=[rng.choice(pool) if distribution else rng.uniform(-1e30,1e30) for _ in range(dim)]
        if distribution==2:values=[rng.randrange(-(1<<24),(1<<24)) for _ in range(dim)]
        expected=[struct.unpack('<f',struct.pack('<f',x))[0] for x in values]
        for literal in (values,json.dumps(values)):
            actual=vector(literal,dim)
            assert struct.pack('<'+str(dim)+'f',*actual)==struct.pack('<'+str(dim)+'f',*expected)
            checks+=1
for bad in (True,None,'1',math.nan,math.inf,-math.inf,1e39,-1e39,10**1000):
    for position in (0,15,31):
        values=[0.]*32;values[position]=bad
        try:vector(values,32)
        except DBError as e:assert e.code=='invalid_vector'
        else:assert False,(bad,position)
        checks+=1
for text in ('[NaN]','[Infinity]','[1e999]','[1,]','[1 2]'):
    try:vector(text,1)
    except DBError as e:assert e.code=='invalid_vector'
    else:assert False,text
    checks+=1
for values in ([True],[],[0.]*33):
    try:vector(values,32)
    except DBError as e:assert e.code=='dimension_mismatch'
    else:assert False
    checks+=1
assert struct.pack('<f',vector('[-0e0]',1)[0])==struct.pack('<f',-0.)
print(json.dumps({'status':'PASS','cases':checks+1,'dimensions':[1,2,3,16,31,32,127,128,129,512,4096],'coverage':['bitwise scalar oracle','list and text inputs','negative zero','subnormal rounding','finite extrema','mixed signed integers','NaN/Infinity','overflow','invalid types','dimension precedence','malformed text']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--entrypoint','python3','minivectordb:codec-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=30)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/vector-codec';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
