#!/usr/bin/env python3
"""Bitwise checked summation, candidate compaction, borrowed-buffer and ABI fallbacks."""
import argparse
from pathlib import Path
import subprocess
import sys

PROBE = r'''
import ctypes,gc,json,math,os,random,struct
from fractions import Fraction
from unittest.mock import patch
from minidb import native
assert native.available()
lib=native._library;rng=random.Random(2026111321)
fast=fallback=rational=cases=screen_checks=0
bits=lambda x:struct.pack('<d',x)
f32=lambda x:struct.unpack('<f',struct.pack('<f',x))[0]
pool=[0.,-0.,2**-149,-2**-149,2**-126,1.,-1.,2**30,-2**30,
      struct.unpack('<f',bytes.fromhex('ffff7f7f'))[0]]
def check(values,query,prove=False):
    global fast,fallback,rational,cases
    dim=len(values);data=struct.pack('<q'+'f'*dim,-7,*values)
    screen=native.Screen(query);terms=(ctypes.c_double*dim)()
    status=lib.mv_squared_checked(data,len(data),dim,screen.query,0,terms)
    expected_terms=[(float(x)-q)*(float(x)-q) for x,q in zip(values,query)]
    expected=math.fsum(expected_terms)
    assert bits(screen.squared(data,0))==bits(expected),(values,query,status)
    if status==2:
        fast+=1;assert bits(terms[0])==bits(expected)
        # Stronger than matching a rounded result: status 2 claims an exact sum.
        assert sum(map(Fraction.from_float,expected_terms),Fraction())==Fraction.from_float(terms[0])
        rational+=1
    else:
        assert status==1
        fallback+=1
        assert [bits(x) for x in terms]==[bits(x) for x in expected_terms]
    if prove:
        assert bits(float(sum(map(Fraction.from_float,expected_terms),Fraction())))==bits(expected)
        rational+=1
    assert lib.mv_squared_terms(data,len(data),dim,screen.query,0,terms)==1
    assert [bits(x) for x in terms]==[bits(x) for x in expected_terms]
    # The query buffer owns a copy, not a borrowed view of the caller's list.
    query[:]=[99.]*dim;gc.collect()
    assert bits(screen.squared(data,0))==bits(expected)
    cases+=1

for dim in (16,17,31,32,127,128,129,256,512,4096):
    for distribution in range(3):
        for _ in range(12):
            def value():
                if distribution==0:return rng.randrange(-64,65)/8
                if distribution==1:return f32(rng.uniform(-10,10))
                return rng.choice(pool)
            check([value() for _ in range(dim)],[value() for _ in range(dim)],dim<=128)
# Half-ULP, swallowed terms, successive small terms, underflow and negative zero.
for tail in (2**-27,2**-26,2**-25,2**-149,2**-126):
    for first in (1.,2**30,pool[-1]):
        check([first]+[tail]*15,[0.]*16,True)
        check([tail]*15+[first],[0.]*16,True)
check([-0.]*16,[0.]*16,True)
assert fast>0 and fallback>0

# Independent exact winner sets: compacted output must retain every tie and
# threshold winner. Exercise dense and 10% masks on both CPU dispatch paths.
for dim in (16,32,128,256):
    for distribution in ('ties','grid','random'):
        query=[0.]*dim
        rows=[[0.]*dim if distribution=='ties' else
               [rng.randrange(-8,9)/8 if distribution=='grid' else f32(rng.uniform(-3,3)) for _ in range(dim)]
              for _ in range(512)]
        data=b''.join(struct.pack('<q'+'f'*dim,i,*v) for i,v in enumerate(rows))
        truth=[math.fsum(x*x for x in v) for v in rows]
        screen=native.Screen(query)
        for mask in (None,bytes(i%10==0 for i in range(512)),bytes(512)):
            eligible=[i for i in range(512) if mask is None or mask[i]]
            for k in (1,10,64):
                for bound in (math.inf,sorted(truth)[100]):
                    got=screen.select(data,bound,mask,k=k)
                    candidates=[i for i in eligible if truth[i]<=bound]
                    cutoff=sorted(truth[i] for i in candidates)[k-1] if len(candidates)>=k else bound
                    expected={i for i in candidates if truth[i]<=cutoff}
                    assert expected.issubset(got) and len(set(got))==len(got)
                    assert set(got).issubset(eligible)
                    guard=0xDEADBEEF;output=(ctypes.c_uint32*514)(*([guard]*514))
                    found=lib.mv_screen_topk(data,len(data),dim,screen.query,bound,mask,k,output)
                    assert list(output[:found])==got and list(output[512:])==[guard,guard]
                    screen_checks+=1

saved=(native._library,native._checked,native._squared_kernel)
class OldABI:
    def __getattr__(self,name):
        if name=='mv_squared_checked':raise AttributeError(name)
        return getattr(lib,name)
try:
    native._checked=False;native._library=None;native._squared_kernel=None
    with patch.object(ctypes,'CDLL',return_value=OldABI()):assert native.available()
    assert native._squared_kernel is lib.mv_squared_terms
    screen=native.Screen([0.]*16);data=struct.pack('<q16f',1,*([.125]*16))
    assert bits(screen.squared(data,0))==bits(.25)
    assert screen.squared(data[:-1],0) is None
    assert screen.squared(data,1) is None
    native._checked=False;native._library=None
    with patch.object(ctypes,'CDLL',side_effect=OSError('missing library')):assert not native.available()
    assert native.Screen([0.]*16).query is None
finally:
    native._library,native._checked,native._squared_kernel=saved
with patch.dict(os.environ,{'MINIDB_NATIVE':'0'}):assert native.Screen([0.]*128).query is None
assert native.Screen([0.]*15).query is None and native.Screen([0.]*4097).query is None
print(json.dumps({'status':'PASS','squared_cases':cases,'exact_fast_returns':fast,'fsum_fallbacks':fallback,
 'rational_sum_checks':rational,'candidate_compaction_checks':screen_checks,
 'simd':bool(lib.mv_screen_simd()),'legacy_terms_abi':True,'missing_optional_symbol_fallback':True,
 'missing_library_fallback':True,'query_buffer_lifetime_and_copy':True}))
'''


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image')
    parser.add_argument('--scalar',action='store_true')
    args=parser.parse_args()
    import os
    env={**os.environ,**({'MINIDB_SIMD':'0'} if args.scalar else {})}
    command=[sys.executable,'-B','-'] if not args.image else [
        'docker','run','--rm','-i','--network=none','--cpus=2','--memory=2g','--memory-swap=2g',
        '--read-only','-e','MINIDB_SIMD='+('0' if args.scalar else '1'),
        '--entrypoint=python3',args.image,'-B','-']
    result=subprocess.run(command,input=PROBE,text=True,capture_output=True,env=env,
                          cwd=Path(__file__).resolve().parents[1],timeout=180)
    print(result.stdout,end='');print(result.stderr,end='',file=sys.stderr)
    raise SystemExit(result.returncode)
