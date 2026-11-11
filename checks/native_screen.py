#!/usr/bin/env python3
"""Independent float32 boundary probe against the actual image's compiled screen."""
import json
from pathlib import Path
import subprocess
PROBE=r'''
import json,math,random,struct
from minidb.native import Screen,available
assert available()
rng=random.Random(20261117)
pool=[0.,-0.,2.**-149,-2.**-149,2.**-126,1.,-1.,2.**30,struct.unpack('<f',bytes.fromhex('ffff7f7f'))[0]]
checks=0
for dim in [16,17,31,32,127,128,129,512,4096]:
    for distribution in range(3):
        def value():
            x=rng.choice(pool) if distribution else rng.uniform(-4,4)
            return struct.unpack('<f',struct.pack('<f',x))[0]
        query=[value() for _ in range(dim)]
        rows=[query[:]]+[[value() for _ in range(dim)] for _ in range(32)]
        rows[1]=rows[0][:]
        data=b''.join(struct.pack('<q'+'f'*dim,i-16,*row) for i,row in enumerate(rows))
        distances=[math.fsum((a-b)*(a-b) for a,b in zip(row,query)) for row in rows]
        bounds=[0.,math.inf]+[sorted(distances)[k] for k in [2,10,20]]
        bounds += [math.nextafter(b,0.) for b in bounds[2:]]
        screen=Screen(query)
        assert screen.query is not None
        for mask in [None,bytes([i%2 for i in range(len(rows))]),bytes(len(rows))]:
            for bound in bounds:
                got=screen.select(data,bound,mask)
                assert got is not None
                assert len(set(got))==len(got)
                assert all(0<=i<len(rows) and (mask is None or mask[i]) for i in got)
                expected={i for i,d in enumerate(distances) if d<=bound and (mask is None or mask[i])}
                assert expected.issubset(got),(dim,distribution,bound,expected-set(got))
                checks+=1
        assert screen.select(data[:-1],1.) is None
assert Screen([0.]*4).query is None
assert Screen([0.]*4097).query is None
import os
os.environ['MINIDB_NATIVE']='0'
assert not available() and Screen([0.]*128).query is None
print(json.dumps({'status':'PASS','screen_checks':checks,'distributions':['uniform','mixed float32 exponents'],'dimensions':[16,17,31,32,127,128,129,512,4096],'coverage':['near threshold','zero/ties','finite extrema/subnormals','mask/empty mask','malformed record length','dimension fallback']}))
'''
if __name__=='__main__':
    result=subprocess.run(['docker','run','--rm','--interactive','--network=none','--memory=64m','--memory-swap=64m','--read-only','--entrypoint','python3','minivectordb:native-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=60)
    assert result.returncode==0,(result.stdout,result.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/native-screen'
    out.mkdir(parents=True,exist_ok=True);(out/'result.json').write_text(result.stdout)
    print(result.stdout,end='')
