"""Optional bounded C screen; all retained distances use the Python reference."""
import ctypes
import os
import math
from array import array
from pathlib import Path

_library = None
_checked = False


def available():
    global _library, _checked
    if os.environ.get('MINIDB_NATIVE') == '0': return False
    if not _checked:
        _checked = True
        try:
            lib = ctypes.CDLL(str(Path(__file__).with_name('libscreen.so')))
            lib.mv_screen_abi.restype = ctypes.c_uint
            if lib.mv_screen_abi() != 5: return False
            lib.mv_squared_terms.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.POINTER(ctypes.c_double)]
            lib.mv_squared_terms.restype = ctypes.c_int
            lib.mv_screen.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_double), ctypes.c_double, ctypes.c_char_p,
                ctypes.POINTER(ctypes.c_uint32)]
            lib.mv_screen.restype = ctypes.c_size_t
            lib.mv_screen_topk.argtypes = lib.mv_screen.argtypes[:-1] + [ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
            lib.mv_screen_topk.restype = ctypes.c_size_t
            if hasattr(lib, 'mv_id_mask'):
                lib.mv_id_mask.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t,
                    ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t,
                    ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t,
                    ctypes.POINTER(ctypes.c_ubyte)]
                lib.mv_id_mask.restype = ctypes.c_size_t
            lib.mv_screen_set_simd.argtypes = [ctypes.c_int]
            lib.mv_screen_set_simd.restype = None
            lib.mv_screen_simd.argtypes = []
            lib.mv_screen_simd.restype = ctypes.c_uint
            lib.mv_screen_set_simd(int(os.environ.get('MINIDB_SIMD') != '0'))
            _library = lib
        except (OSError, AttributeError):
            _library = None
    return _library is not None


class Screen:
    def __init__(self, query):
        self.dimension = len(query)
        self.query = (ctypes.c_double * len(query))(*query) if 16 <= len(query) <= 4096 and available() else None
        self.capacity = 0
        self.output = None
        self.terms = self.terms_pointer = None

    @staticmethod
    def pointer(data):
        if isinstance(data, bytes): return data, None
        try:
            owner = ctypes.c_char.from_buffer(data)
        except (TypeError, ValueError, BufferError):
            return bytes(data), None
        return ctypes.c_char_p(ctypes.addressof(owner)), owner

    def squared(self, data, position, pointer=None):
        if self.query is None: return None
        if self.terms is None:
            self.terms = array('d', [0.]) * self.dimension
            self.terms_pointer = (ctypes.c_double * self.dimension).from_buffer(self.terms)
        if pointer is None: pointer, owner = self.pointer(data)
        if not _library.mv_squared_terms(pointer, len(data), self.dimension, self.query, position, self.terms_pointer):
            return None
        return math.fsum(self.terms)

    def select(self, data, bound, mask=None, k=None, pointer=None):
        if self.query is None: return None
        count = len(data) // (8 + self.dimension * 4)
        if mask is not None and len(mask) != count: raise ValueError('invalid native mask')
        if count > self.capacity:
            self.output = (ctypes.c_uint32 * count)()
            self.capacity = count
        if pointer is None: pointer, owner = self.pointer(data)
        if k is None:
            result = _library.mv_screen(pointer, len(data), self.dimension, self.query, bound, mask, self.output)
        else:
            result = _library.mv_screen_topk(pointer, len(data), self.dimension, self.query, bound, mask, k, self.output)
        if result == ctypes.c_size_t(-1).value: return None
        if result > count: raise RuntimeError('invalid native result count')
        return self.output[:result]


def id_mask(data, dimension, allowed, excluded, pointer=None):
    if not available(): return None
    lib = _library
    if not hasattr(lib, 'mv_id_mask'): return None
    stride = 8 + dimension * 4
    if len(data) % stride: return None
    count = len(data) // stride
    if not count: return b''
    if allowed is not None and not len(allowed): return bytes(count)
    if pointer is None: pointer, owner = Screen.pointer(data)
    allowed_pointer = allowed_count = None
    if allowed is not None and len(allowed):
        allowed_count = len(allowed)
        allowed_pointer = (ctypes.c_int64 * allowed_count).from_buffer(allowed)
    excluded_pointer = excluded_count = None
    if excluded is not None and len(excluded):
        excluded_count = len(excluded)
        excluded_pointer = (ctypes.c_int64 * excluded_count).from_buffer(excluded)
    result = bytearray(count)
    output = (ctypes.c_ubyte * count).from_buffer(result)
    found = lib.mv_id_mask(pointer, len(data), dimension,
                           allowed_pointer, allowed_count or 0,
                           excluded_pointer, excluded_count or 0, output)
    return bytes(result) if found == count else None
