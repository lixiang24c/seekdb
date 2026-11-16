#!/usr/bin/env python3
"""Bit-exact codec tests, including exhaustive int16 coefficients and half values."""
import argparse,ctypes,hashlib,json,math,random,struct,os
from pathlib import Path

def main():
 p=argparse.ArgumentParser();p.add_argument('--library',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 lib=ctypes.CDLL(a.library);lib.mv_codec_width.argtypes=[ctypes.c_size_t,ctypes.c_uint32];lib.mv_codec_width.restype=ctypes.c_size_t
 lib.mv_codec_encode.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_void_p];lib.mv_codec_decode.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p]
 lib.mv_screen_set_simd(0 if os.getenv("MINIDB_SIMD")=="0" else 1)
 simd=lib.mv_screen_simd()
 cases=accepted=rejected=0;digest=hashlib.sha256();rng=random.Random(42)
 def check(raw,descriptor,required=False):
  nonlocal cases,accepted,rejected
  dim=len(raw)//4;width=lib.mv_codec_width(dim,descriptor);assert width<20000
  encoded=ctypes.create_string_buffer(width);out=ctypes.create_string_buffer(len(raw));ok=lib.mv_codec_encode(raw,dim,descriptor,encoded);cases+=1
  if not ok:rejected+=1;assert not required,(dim,descriptor);return
  accepted+=1;assert lib.mv_codec_decode(encoded,dim,out)==1
  assert out.raw==raw,{'dimension':dim,'descriptor':descriptor,'raw':raw.hex(),'decoded':out.raw.hex()};digest.update(raw)
  lib.mv_screen_set_simd(0 if simd else 1);assert lib.mv_codec_decode(encoded,dim,out)==1 and out.raw==raw;lib.mv_screen_set_simd(simd)
 # Every legal signed16 coefficient at all seven scales. -32768 is the raw exception sentinel.
 inverse=[1.,.1,.01,.001,.0001,.00001,.000001]
 for scale,factor in enumerate(inverse):
  for begin in range(-32767,32768,128):
   values=[-0. if n==-32767 else n*factor for n in range(begin,min(begin+128,32768))]
   check(struct.pack('<'+'f'*len(values),*values),0x01000002|(scale<<8),True)
 # Every finite half, including both zeros and subnormal endpoints.
 halves=[struct.unpack('<e',struct.pack('<H',n))[0] for n in range(65536) if n&0x7c00!=0x7c00]
 for start in range(0,len(halves),128):
  v=halves[start:start+128];check(struct.pack('<'+'f'*len(v),*v),0x01000003,True)
 for dim in (1,3,7,8,31,32,127,128,129,512,4096):
  for scale,factor in enumerate(inverse):
   for _ in range(4):
    v=[rng.randrange(-8388607,8388608)*factor for _ in range(dim)];v[0]=-0.;check(struct.pack('<'+'f'*dim,*v),0x01000001|(scale<<8),True)
  for _ in range(16):
   bits=[rng.getrandbits(32) for _ in range(dim)];bits=[b if b&0x7f800000!=0x7f800000 else 0x7f7fffff for b in bits]
   raw=struct.pack('<'+'I'*dim,*bits)
   for mode in (1,2,3):check(raw,0x01000000|mode)
  slots=max(4,(dim+31)//32);bits=[0x80000000]*dim
  for j,b in enumerate([1,0x80000001,0x7f7fffff,0x00800000][:min(dim,slots)]):bits[j]=b
  check(struct.pack('<'+'I'*dim,*bits),0x01000402,True)
 for mode in (1,2,3):
  for b in (0x7f800000,0xff800000,0x7fc00000):check(struct.pack('<I',b)*128,0x01000000|mode)
 for desc in (0,0x01000702,0x02000002,0x01010002,0x01000004):
  out=ctypes.create_string_buffer(1024);assert not lib.mv_codec_encode(bytes(512),128,desc,out)
 a.output.write_text(json.dumps({'status':'PASS','cases':cases,'accepted':accepted,'rejected':rejected,'accepted_raw_digest':digest.hexdigest(),'simd':lib.mv_screen_simd(),'coverage':['all legal int16 coefficients / scales 0..6','all finite binary16','int24 random coefficients / scales 0..6','bit patterns, negative zero, subnormal, extremes, exceptions and nonfinite rejection','unaligned SIMD tails and dimensions 1..4096']},indent=2)+'\n')
if __name__=='__main__':main()
