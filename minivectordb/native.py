"""Optional self-built math kernel; NumPy remains the portable fallback."""
import ctypes
import os
from pathlib import Path

_library=None
_checked=False


def available():
    global _library,_checked
    if os.environ.get('MINIVECTOR_NATIVE')=='0':return False
    if not _checked:
        _checked=True
        try:
            library=ctypes.CDLL(str(Path(__file__).with_name('libminivector.so')))
            function=library.mv_topk
            function.restype=ctypes.c_size_t
            function.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_size_t,ctypes.c_size_t,
                               ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int,
                               ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p]
            _library=library
        except OSError:
            _library=None
    if _library is None:
        if os.environ.get('MINIVECTOR_REQUIRE_NATIVE')=='1':
            raise RuntimeError('required native kernel could not be loaded')
        return False
    return True


def scan(entries, query, mask, k, use_simd=True):
    if not available():return None
    import numpy as np
    k=min(k,len(entries))
    distance=np.empty(k,dtype=np.float64);keys=np.empty(k,dtype=np.int64);offsets=np.empty(k,dtype=np.uint64)
    selected=None
    if not isinstance(mask,slice):
        if isinstance(mask,list):
            selected=np.zeros(len(entries),dtype=np.uint8);selected[mask]=1
        else:selected=np.ascontiguousarray(mask,dtype=np.uint8)
    size=_library.mv_topk(entries.ctypes.data,len(entries),entries.strides[0],len(query),query.ctypes.data,
                          selected.ctypes.data if selected is not None else None,k,int(use_simd),
                          distance.ctypes.data,keys.ctypes.data,offsets.ctypes.data)
    if size==ctypes.c_size_t(-1).value:raise MemoryError('native TopK allocation failed')
    return ((float(distance[i]),int(keys[i]),int(offsets[i])) for i in range(size))
