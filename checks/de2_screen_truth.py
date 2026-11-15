#!/usr/bin/env python3
"""Candidate inclusion/interval truth, independent math.fsum, no timings."""
import argparse
import ctypes as C
import json
import math
from pathlib import Path
import random
import struct
import traceback


def f32(x):return struct.unpack('<f',struct.pack('<f',x))[0]

def main():
    p=argparse.ArgumentParser();p.add_argument('--library',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False);report={'status':'RUNNING','oracle':'binary64 rounded products accumulated with independent math.fsum','groups':0,'refinements':0,'certified':0,'quant_rejected':0}
    lib=C.CDLL(a.library);ptr=C.c_void_p;size=C.c_size_t;dp=C.POINTER(C.c_double)
    lib.mv_screen_set_simd(1);lib.mv_screen_set_float(1)
    lib.mv_quant_pack_block_row.argtypes=[ptr,ptr,size,dp,dp];lib.mv_quant_pack_block_row.restype=C.c_int
    lib.mv_quant_prepare.argtypes=[dp,C.c_double,C.c_double,ptr];lib.mv_quant_prepare.restype=C.c_int
    lib.mv_quant_topk_blocks.argtypes=[ptr,size,ptr,size,size,ptr];lib.mv_quant_topk_blocks.restype=size
    lib.mv_quant_topk_bound_blocks.argtypes=[ptr,size,ptr,ptr,size,size,ptr];lib.mv_quant_topk_bound_blocks.restype=size
    lib.mv_refine_block_candidates.argtypes=[ptr,ptr,dp,size,ptr,size,dp,ptr];lib.mv_refine_block_candidates.restype=size
    lib.mv_block_kernel.restype=C.c_char_p
    try:
        if not lib.mv_quant_available():report['status']='UNSUPPORTED_CPU';return
        report['kernel']=lib.mv_block_kernel().decode();rng=random.Random(424344)
        for trial in range(240):
            n=(17,65,129,273,1153)[trial%5];slots=(n+15)//16*16;kind=trial%12
            def vector(i):
                if kind==0:return [0.]*128
                if kind==1:return [f32(1.+rng.randrange(-2,3)*2**-23) for _ in range(128)]
                if kind==2:return [rng.choice([-1,0,1])*2**-149 for _ in range(128)]
                if kind==3:return [rng.choice([-1,0,1])*f32(3.4028234663852886e38) for _ in range(128)]
                scale=1e30 if kind==4 and i%17==0 else 2**-80 if kind==5 else 2**80 if kind==6 else 1.
                v=[f32(rng.uniform(-1,1)*scale) for _ in range(128)]
                if kind==7:
                    norm=math.sqrt(math.fsum(x*x for x in v));v=[f32(x/norm) for x in v]
                if kind==11:v=[f32(1e-10)]*128
                return v
            rows=[vector(i) for i in range(n)];query=vector(-1)
            if kind in (7,8):query=[0.]*128
            if kind in (9,10):query=[x if j<2*(trial%18) else 0. for j,x in enumerate(query)]
            raw=b''.join(struct.pack('<q128f',i-n//2,*v) for i,v in enumerate(rows));view=C.create_string_buffer(raw);tiles=C.create_string_buffer(slots*144)
            bounds=(C.c_double*(slots//8))();norm=0.;error=0.
            for i in range(n):
                bn=C.c_double(bounds[2*(i//16)]);be=C.c_double(bounds[2*(i//16)+1])
                assert lib.mv_quant_pack_block_row(C.byref(view,i*520),tiles,i,C.byref(bn),C.byref(be))
                bounds[2*(i//16)]=bn.value;bounds[2*(i//16)+1]=be.value;norm=max(norm,bn.value);error=max(error,be.value)
            q=(C.c_double*128)(*query);prepared=lib.mv_quant_prepare(q,norm,error,C.byref(view,8))
            active=list(range(n));ranges=[0,n]
            if trial%3==1 and n>65:active=list(range(17))+list(range(48,n));ranges=[0,17,48,n-48]
            rg=(C.c_uint32*len(ranges))(*ranges);k=min((1,10,32,127,1024)[trial%5],len(active))
            truth={i:math.fsum((x-y)*(x-y) for x,y in zip(rows[i],query)) for i in active};rank=sorted(active,key=lambda i:(truth[i],i));wanted=set(rank[:k])
            positions=(C.c_uint32*slots)()
            # Refined results must retain all true winners even when no order
            # can be certified. This also checks unaligned record addresses.
            for four_input in (False,True):
                offset=1 if four_input else 0
                rview=C.create_string_buffer(b'x'*offset+raw);records=(C.c_uint64*n)(*(offset+i*520 for i in range(n)))
                selected=(C.c_uint32*len(active))(*active);scores=(C.c_double*1024)();cert=C.c_int()
                got=lib.mv_refine_block_candidates(rview,records,q,k,selected,len(active),scores,C.byref(cert))
                assert got<=len(active) and wanted.issubset(set(selected[:got])),('refine inclusion',trial)
                if cert.value:
                    assert list(selected[:got])==rank[:k],('certified ranks',trial)
                    eps=64*2**-52*129
                    for j,i in enumerate(selected[:got]):assert math.nextafter(scores[j]*(1-eps)-2**-1022,-math.inf)<=truth[i]<=math.nextafter(scores[j]*(1+eps)+2**-1022,math.inf)
                    report['certified']+=1
                report['refinements']+=1
            if not prepared:report['quant_rejected']+=1;continue
            for local in (False,True):
                got=lib.mv_quant_topk_bound_blocks(tiles,slots,bounds,rg,len(ranges)//2,k,positions) if local else lib.mv_quant_topk_blocks(tiles,slots,rg,len(ranges)//2,k,positions)
                assert got<=len(active) and wanted.issubset(set(positions[:got])),('coarse inclusion',trial,local)
                assert set(positions[:got]).issubset(active)
                report['groups']+=1
        assert report['groups'] and report['certified']
        report['status']='PASS'
    except BaseException as e:
        report.update(status='FAIL',error=repr(e),traceback=traceback.format_exc(),trial=locals().get('trial'))
        if 'rows' in locals():(a.output/'repro.json').write_text(json.dumps({'rows':rows,'query':query,'active':active,'k':k}))
        raise
    finally:(a.output/'report.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)
if __name__=='__main__':main()
