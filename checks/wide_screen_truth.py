#!/usr/bin/env python3
"""Independently check dynamic screening retains every exact TopK winner."""
import argparse
import ctypes as C
import json
import math
from pathlib import Path
import random
import struct
import traceback
from native_validation import f32


def main():
    p=argparse.ArgumentParser();p.add_argument('--library',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
    r={'status':'RUNNING','groups':0,'screened':0,'fallbacks':0,'by_dimension':{},'int32_bound':512*127*32767};lib=C.CDLL(a.library);ptr=C.c_void_p;size=C.c_size_t;dp=C.POINTER(C.c_double)
    lib.mv_screen_set_simd(1);lib.mv_screen_set_float(1);lib.mv_dynamic_available.argtypes=[size];lib.mv_dynamic_available.restype=C.c_int
    lib.mv_dynamic_pack_row.argtypes=[ptr,size,ptr,size,dp,dp];lib.mv_dynamic_pack_row.restype=C.c_int
    lib.mv_dynamic_topk.argtypes=[ptr,size,size,dp,C.c_double,C.c_double,ptr,ptr,size,size,ptr];lib.mv_dynamic_topk.restype=size
    lib.mv_refine_dynamic_candidates.argtypes=[ptr,ptr,dp,size,size,ptr,size,dp,C.POINTER(C.c_int)];lib.mv_refine_dynamic_candidates.restype=size
    lib.mv_block_kernel.restype=C.c_char_p
    try:
        if not lib.mv_dynamic_available(512):r['status']='UNSUPPORTED_CPU';return
        r['kernel']=lib.mv_block_kernel().decode();rng=random.Random(424344)
        for dim in (16,496,512,528,768,1024,4080,4096):
            accepted=0
            for trial in range(20):
                n=(17,65,273)[trial%3];slots=(n+15)//16*16;kind=trial%10
                def vector(i):
                    if kind==0:return [0.]*dim
                    if kind==1:return [f32(1+rng.randrange(-2,3)*2**-23) for _ in range(dim)]
                    if kind==2:return [rng.choice([-1.,0.,1.])*2**-149 for _ in range(dim)]
                    if kind==3:return [rng.choice([-1.,0.,1.])*f32(3.4028234663852886e38) for _ in range(dim)]
                    if kind==4:return [(-1. if i%2 else 1.)]*dim
                    scale=2**-80 if kind==5 else 2**80 if kind==6 else 1e30 if kind==7 and i%17==0 else 1.
                    return [f32(rng.gauss(0,1)*scale) for _ in range(dim)]
                rows=[vector(i) for i in range(n)];query=vector(-1)
                if kind in (0,1,8):query=[0.]*dim
                raw=C.create_string_buffer(b''.join(struct.pack('<q'+str(dim)+'f',i,*v) for i,v in enumerate(rows)));tiles=C.create_string_buffer(slots*(dim+16));norm=C.c_double();error=C.c_double()
                for i in range(n):assert lib.mv_dynamic_pack_row(C.byref(raw,i*(8+4*dim)),dim,tiles,i,C.byref(norm),C.byref(error))
                active=list(range(n));ranges=[0,n]
                if trial%2 and n>65:active=list(range(17))+list(range(48,n));ranges=[0,17,48,n-48]
                q=(C.c_double*dim)(*query);rg=(C.c_uint32*len(ranges))(*ranges);selected=(C.c_uint32*slots)();k=min((1,10,64)[trial%3],len(active))
                distances={i:math.fsum((x-y)*(x-y) for x,y in zip(rows[i],query)) for i in active};wanted=set(sorted(active,key=lambda i:(distances[i],i))[:k])
                got=lib.mv_dynamic_topk(tiles,slots,dim,q,norm.value,error.value,C.byref(raw,8),rg,len(ranges)//2,k,selected)
                r['groups']+=1
                if got==C.c_size_t(-1).value:r['fallbacks']+=1;continue
                actual=set(selected[:got]);assert got<=len(active) and wanted<=actual and actual<=set(active),(dim,trial,wanted-actual)
                r['screened']+=1;accepted+=1
                offsets=(C.c_uint64*n)(*(i*(8+4*dim) for i in range(n)));scores=(C.c_double*1024)();cert=C.c_int()
                kept=lib.mv_refine_dynamic_candidates(raw,offsets,q,dim,k,selected,got,scores,C.byref(cert))
                if kept!=C.c_size_t(-1).value:
                    narrowed=list(selected[:kept]);assert wanted<=set(narrowed),(dim,trial,'refine lost exact winner')
                    r['refine_checks']=r.get('refine_checks',0)+1
                    if cert.value:
                        assert narrowed==sorted(active,key=lambda i:(distances[i],i))[:k],(dim,trial,'false certification')
                        r['certified']=r.get('certified',0)+1
            assert accepted>0;r['by_dimension'][str(dim)]={'screened':accepted,'groups':20}
        assert r['int32_bound']<2**31;r['status']='PASS'
    except BaseException as e:r.update(status='FAIL',error=repr(e),traceback=traceback.format_exc());raise
    finally:(a.output/'report.json').write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
if __name__=='__main__':main()
