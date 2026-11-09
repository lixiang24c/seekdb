#!/usr/bin/env python3
"""Short paired comparison of two native kernels on identical vectors/queries."""
import argparse
import ctypes
import json
import statistics
import time
from pathlib import Path
import numpy as np

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline',required=True)
parser.add_argument('--candidate',default='minivectordb/libminivector.so')
parser.add_argument('--output',required=True)
parser.add_argument('--quantized',action='store_true')
parser.add_argument('--distribution',choices=('normal','uniform'),default='normal')
args=parser.parse_args()
old=ctypes.CDLL(str(Path(args.baseline).resolve()))
new=ctypes.CDLL(str(Path(args.candidate).resolve()))
signature=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_size_t,ctypes.c_size_t,
           ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int,
           ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
for lib in (old,new):
    lib.mv_topk.argtypes=signature;lib.mv_topk.restype=ctypes.c_size_t
    if hasattr(lib,'mv_topk_selected'):
        lib.mv_topk_selected.argtypes=signature[:6]+[ctypes.c_size_t]+signature[6:]
        lib.mv_topk_selected.restype=ctypes.c_size_t
new.mv_topk_selected.argtypes=signature[:6]+[ctypes.c_size_t]+signature[6:]
new.mv_topk_selected.restype=ctypes.c_size_t
rng=np.random.default_rng(20261109);n=10000;dim=128
rows=np.zeros(n,dtype=[('pk','<i8'),('offset','<u8'),('vector','<f4',(dim,))])
rows['pk']=np.arange(n)
sample=rng.normal if args.distribution=='normal' else lambda size:rng.uniform(-1,1,size=size)
rows['vector']=sample(size=(n,dim))
queries=sample(size=(300,dim));distances=np.empty(10)
keys=np.empty(10,dtype=np.int64);offsets=np.empty(10,dtype=np.uint64)
codes=None
if args.quantized:
    codes=np.empty((n,dim),dtype=np.uint8);minimum=ctypes.c_double();step=ctypes.c_double()
    new.mv_quantize.argtypes=signature[:4]+[ctypes.c_void_p]*3
    new.mv_quantize.restype=ctypes.c_int
    assert new.mv_quantize(rows.ctypes.data,n,rows.strides[0],dim,ctypes.byref(minimum),ctypes.byref(step),codes.ctypes.data)
    new.mv_topk_quantized.argtypes=signature[:5]+[ctypes.c_void_p,ctypes.c_double,ctypes.c_double,
        ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]+signature[6:]
    new.mv_topk_quantized.restype=ctypes.c_size_t

def run(lib,mask,positions):
    results=[];start=time.perf_counter()
    for query in queries:
        arguments=[rows.ctypes.data,n,rows.strides[0],dim,query.ctypes.data]
        if lib is new and codes is not None and (positions is None or len(positions)>=512):
            selection=positions if positions is not None else mask
            arguments.extend((codes.ctypes.data,minimum.value,step.value,
                selection.ctypes.data if selection is not None else None,
                len(positions) if positions is not None else n,int(positions is not None)))
            function=lib.mv_topk_quantized
        elif positions is not None and (lib is new or (args.quantized and hasattr(lib,'mv_topk_selected'))):
            arguments.extend((positions.ctypes.data,len(positions)));function=lib.mv_topk_selected
        else:
            arguments.append(mask.ctypes.data if mask is not None else None);function=lib.mv_topk
        arguments.extend((10,1,distances.ctypes.data,keys.ctypes.data,offsets.ctypes.data))
        count=function(*arguments)
        if count==ctypes.c_size_t(-1).value:raise MemoryError('native allocation failed')
        results.append(keys[:count].tolist())
    return time.perf_counter()-start,results

report={'scope':'native kernel only; paired medians of 3 rounds, 300 queries per round',
        'rows':n,'dimensions':dim,'k':10,'seed':20261109,
        'quantized_screen':args.quantized,'distribution':args.distribution,
        'baseline_sparse_positions':args.quantized and hasattr(old,'mv_topk_selected'),'cases':{}}
for stride in (1,10,100):
    mask=None if stride==1 else (np.arange(n)%stride==0).astype(np.uint8)
    positions=None if stride==1 else np.flatnonzero(mask).astype(np.uint32)
    run(old,mask,positions);run(new,mask,positions);before=[];after=[]
    for iteration in range(3):
        if iteration%2:
            nt,nr=run(new,mask,positions);ot,orr=run(old,mask,positions)
        else:
            ot,orr=run(old,mask,positions);nt,nr=run(new,mask,positions)
        assert nr==orr,'candidate IDs differ'
        before.append(ot);after.append(nt)
    baseline=statistics.median(before);candidate=statistics.median(after)
    report['cases'][str(100//stride)+'% candidates']={
        'baseline_seconds':baseline,'candidate_seconds':candidate,'speedup':baseline/candidate,
        'matching_ids':True,'baseline_rounds':before,'candidate_rounds':after}
Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
