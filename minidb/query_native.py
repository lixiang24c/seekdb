"""One C++ exact heap per query; Python supplies visible blocks or VA positions."""
import ctypes
import os
from .native import Screen
from .native_storage import vector_parser

_configured = False


class NativeTopK:
    scans = files = 0

    def __init__(self, screen, k):
        global _configured
        self.handle = None
        self.lib = None
        self.k = k
        if screen.query is None or os.environ.get('MINIDB_CPP_TOPK') == '0': return
        if not vector_parser.available() or not hasattr(vector_parser.lib, 'mv_topk_new'): return
        self.lib = vector_parser.lib
        if not _configured:
            lib = self.lib
            lib.mv_topk_new.argtypes = [ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t]
            lib.mv_topk_new.restype = ctypes.c_void_p
            lib.mv_topk_free.argtypes = [ctypes.c_void_p]
            lib.mv_topk_free.restype = None
            lib.mv_topk_scan.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
            lib.mv_topk_scan.restype = ctypes.c_int
            lib.mv_topk_file.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint64)]
            lib.mv_topk_file.restype = ctypes.c_int
            lib.mv_topk_result.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
            lib.mv_topk_result.restype = ctypes.c_size_t
            _configured = True
        self.handle = self.lib.mv_topk_new(screen.query, screen.dimension, k)
        self.stride = 8 + screen.dimension * 4

    def close(self):
        if self.handle:
            self.lib.mv_topk_free(self.handle)
            self.handle = None

    def scan(self, data, mask, pointer):
        if len(data) % self.stride or (mask is not None and len(mask) != len(data) // self.stride):
            raise OSError('invalid native TopK block')
        if pointer is None: pointer, owner = Screen.pointer(data)
        if self.lib.mv_topk_scan(self.handle, pointer, len(data), mask): raise OSError('native TopK scan failed')
        type(self).scans += 1

    def file(self, path, positions):
        ids = (ctypes.c_uint32 * len(positions))(*positions)
        read_bytes = ctypes.c_uint64()
        if self.lib.mv_topk_file(self.handle, os.fsencode(path), ids, len(ids), ctypes.byref(read_bytes)):
            raise OSError('native TopK candidate read failed')
        type(self).files += 1
        return read_bytes.value

    def scores(self):
        ids = (ctypes.c_int64 * self.k)()
        scores = (ctypes.c_double * self.k)()
        count = self.lib.mv_topk_result(self.handle, ids, scores, self.k)
        if count > self.k: raise OSError('native TopK result failed')
        return {ids[i]: scores[i] for i in range(count)}
