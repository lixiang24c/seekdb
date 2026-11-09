"""Optional self-built math kernel; NumPy remains the portable fallback."""
import ctypes
import os
import struct
from pathlib import Path

_library=None
_checked=False
QUANT_HEADER=struct.Struct('<8sQIdd')
QUANT_MAGIC=b'MVDBQ01\n'


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
            selected=library.mv_topk_selected
            selected.restype=ctypes.c_size_t
            selected.argtypes=function.argtypes[:6]+[ctypes.c_size_t]+function.argtypes[6:]
            quantized=library.mv_topk_quantized
            quantized.restype=ctypes.c_size_t
            quantized.argtypes=function.argtypes[:5]+[ctypes.c_void_p,ctypes.c_double,ctypes.c_double,
                ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]+function.argtypes[6:]
            quantize=library.mv_quantize
            quantize.restype=ctypes.c_int
            quantize.argtypes=function.argtypes[:4]+[ctypes.c_void_p]*3
            _library=library
        except (OSError,AttributeError):
            _library=None
    if _library is None:
        if os.environ.get('MINIVECTOR_REQUIRE_NATIVE')=='1':
            raise RuntimeError('required native kernel could not be loaded')
        return False
    return True


def scan(entries, query, mask, k, use_simd=True, quantization=None):
    """Scan all rows, a uint8 bitmap, or sorted uint32 candidate positions."""
    if not available():return None
    import numpy as np
    sparse=isinstance(mask,np.ndarray) and mask.dtype==np.dtype('uint32')
    k=min(k,len(mask) if sparse else len(entries))
    distance=np.empty(k,dtype=np.float64);keys=np.empty(k,dtype=np.int64);offsets=np.empty(k,dtype=np.uint64)
    selected=None
    if not isinstance(mask,slice):
        if isinstance(mask,list):
            selected=np.zeros(len(entries),dtype=np.uint8);selected[mask]=1
        else:selected=np.ascontiguousarray(mask,dtype=np.uint32 if sparse else np.uint8)
    arguments=[entries.ctypes.data,len(entries),entries.strides[0],len(query),query.ctypes.data,
               selected.ctypes.data if selected is not None else None]
    if quantization is not None and (len(mask) if sparse else len(entries))>=512 and k<=(len(mask) if sparse else len(entries))//4:
        codes,minimum,step=quantization
        arguments=arguments[:5]+[codes.ctypes.data,minimum,step,arguments[5],
                                  len(selected) if sparse else len(entries),int(sparse)]
        arguments.extend((k,int(use_simd),distance.ctypes.data,keys.ctypes.data,offsets.ctypes.data))
        size=_library.mv_topk_quantized(*arguments)
        if size==ctypes.c_size_t(-1).value:raise MemoryError('native TopK allocation failed')
        return ((float(distance[i]),int(keys[i]),int(offsets[i])) for i in range(size))
    if sparse:arguments.append(len(selected))
    arguments.extend((k,int(use_simd),distance.ctypes.data,keys.ctypes.data,offsets.ctypes.data))
    size=(_library.mv_topk_selected if sparse else _library.mv_topk)(*arguments)
    if size==ctypes.c_size_t(-1).value:raise MemoryError('native TopK allocation failed')
    return ((float(distance[i]),int(keys[i]),int(offsets[i])) for i in range(size))


def build_quantized(source, destination, dimension, byte_limit):
    """Build a bounded immutable byte-code sidecar; return whether one was built."""
    size=source.stat().st_size;stride=16+4*dimension
    if dimension<32 or dimension>4096 or size>byte_limit or size//stride<256 or not available():
        return False
    try:import numpy as np
    except ImportError:return False
    count=size//stride
    rows=np.memmap(source,dtype=np.uint8,mode='r')
    try:
        with destination.open('x+b') as output:
            output.truncate(QUANT_HEADER.size+count*dimension)
            codes=np.memmap(destination,dtype=np.uint8,offset=QUANT_HEADER.size,mode='r+',shape=(count*dimension,))
            try:
                minimum=ctypes.c_double();step=ctypes.c_double()
                valid=_library.mv_quantize(rows.ctypes.data,count,stride,dimension,
                    ctypes.byref(minimum),ctypes.byref(step),codes.ctypes.data)
                if valid:
                    codes.flush()
                    output.write(QUANT_HEADER.pack(QUANT_MAGIC,count,dimension,minimum.value,step.value))
                    output.flush();os.fsync(output.fileno())
            finally:codes._mmap.close()
        if not valid:destination.unlink()
        return bool(valid)
    finally:rows._mmap.close()
