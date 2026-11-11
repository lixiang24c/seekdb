"""Opt-in, one-table bounded graph cache; persisted flat indexes stay authoritative."""
import ctypes
import os
import struct
import time

from . import native
from .storage import INDEX_MAGIC


class GraphCache:
    def __init__(self):
        self.enabled = os.environ.get('MINIDB_ANN_GRAPH') == '1'
        self.handle = None
        self.key = self.name = self.dimension = None
        self.lib = None
        self.build_seconds = 0.
        self.queries = self.hits = self.fallbacks = 0
        try: self.ef = min(2048, max(64, int(os.environ.get('MINIDB_ANN_EF', '512'))))
        except ValueError: self.ef = 512
        if self.enabled and native.available():
            lib = native._library
            lib.mv_graph_new.argtypes = [ctypes.c_size_t] * 3
            lib.mv_graph_new.restype = ctypes.c_void_p
            lib.mv_graph_append.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
            lib.mv_graph_append.restype = ctypes.c_int
            lib.mv_graph_build.argtypes = [ctypes.c_void_p]
            lib.mv_graph_build.restype = ctypes.c_int
            lib.mv_graph_free.argtypes = [ctypes.c_void_p]
            lib.mv_graph_free.restype = None
            lib.mv_graph_search.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int64)]
            lib.mv_graph_search.restype = ctypes.c_size_t
            self.lib = lib

    @staticmethod
    def signature(name, table, store):
        return (name, table['count'], tuple((p['file'], p.get('index_file'), tuple(p.get('excluded', ()))) for p in store.segments(table)))

    def close(self):
        if self.handle is not None: self.lib.mv_graph_free(self.handle)
        self.handle = self.key = self.name = self.dimension = None

    def validate(self, store):
        if self.handle is None: return
        table = store.tables.get(self.name)
        if table is None or self.signature(self.name, table, store) != self.key: self.close()

    def prepare(self, name, table, store):
        if not self.lib or not table.get('indexes') or not 1024 <= table['count'] <= 20000: return False
        dim = next(c['n'] for c in table['cols'] if c['type'] == 'VECTOR')
        if not 16 <= dim <= 256: return False
        key = self.signature(name, table, store)
        if self.handle is not None:
            if key == self.key: return True
            return False  # At most one table, never unbounded graph residency.
        started = time.perf_counter()
        handle = self.lib.mv_graph_new(table['count'], dim, 16 << 20)
        if not handle: return False
        fmt, key_format = struct.Struct('<q' + str(dim) + 'f'), struct.Struct('<q')
        try:
            for path, excluded in store.index_segments(table):
                with open(path, 'rb') as f:
                    if f.read(8) != INDEX_MAGIC: raise OSError('invalid graph source header')
                    while data := f.read(fmt.size * max(1, 65536 // fmt.size)):
                        if len(data) % fmt.size: raise OSError('incomplete graph source')
                        mask = bytes(key_format.unpack_from(data, i)[0] not in excluded for i in range(0, len(data), fmt.size)) if excluded else None
                        if not self.lib.mv_graph_append(handle, data, len(data), mask): raise OSError('invalid graph row count')
            if not self.lib.mv_graph_build(handle): return False
            self.handle, handle = handle, None
            self.key, self.name, self.dimension = key, name, dim
            return True
        finally:
            if handle: self.lib.mv_graph_free(handle)
            self.build_seconds += time.perf_counter() - started

    def search(self, table, query, k, store):
        if self.handle is None or len(query) != self.dimension or not native.available() or not 1 <= k <= 64: return None
        if self.signature(self.name, table, store) != self.key: return None
        self.queries += 1
        q = (ctypes.c_double * len(query))(*query)
        output = (ctypes.c_int64 * k)()
        count = self.lib.mv_graph_search(self.handle, q, k, max(k + 1, self.ef), output)
        if count != k:
            self.fallbacks += 1
            return None
        ids = list(output)
        if len(set(ids)) != k:
            self.fallbacks += 1
            return None
        self.hits += 1
        return ids
