"""C ABI for C++ segment construction; Python owns transaction publication."""
import ctypes
from array import array
import os
import re
from pathlib import Path


class Source(ctypes.Structure):
    _fields_ = [('rows', ctypes.c_char_p), ('vectors', ctypes.c_char_p),
                ('count', ctypes.c_uint64), ('excluded', ctypes.POINTER(ctypes.c_int64)),
                ('excluded_count', ctypes.c_size_t)]


class Segment(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint64), ('key_offset', ctypes.c_uint64),
                ('minimum', ctypes.c_int64), ('maximum', ctypes.c_int64)]


class Column(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char_p), ('kind', ctypes.c_uint32), ('width', ctypes.c_uint32)]


class PreparedInsert:
    def __init__(self, lib, handle):
        self.lib, self.handle = lib, handle
        try:
            self.count = lib.mv_insert_count(handle)
            self.ids = array('q', [0]) * self.count
            ids = (ctypes.c_int64 * self.count).from_buffer(self.ids)
            if lib.mv_insert_ids(handle, ids, self.count): raise RuntimeError('invalid native primary keys')
            size = ctypes.c_size_t()
            pointer = lib.mv_insert_data(handle, ctypes.byref(size))
            self.payload = (ctypes.c_ubyte * size.value).from_address(ctypes.addressof(pointer.contents))
        except BaseException:
            self.close(); raise

    def __len__(self): return self.count

    def close(self):
        if self.handle:
            self.payload = None
            self.lib.mv_insert_free(self.handle)
            self.handle = None


_INSERT_TABLE = re.compile(r'\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z_0-9]*)(?=\s|\()', re.I | re.ASCII)


class NativeStorage:
    def __init__(self):
        self.lib = None
        self.checked = False
        self.merges = 0
        self.parsed_vectors = 0
        self.prepared_inserts = 0

    def available(self):
        if os.environ.get('MINIDB_CPP_STORAGE') == '0' or os.environ.get('MINIDB_NATIVE') == '0': return False
        if not self.checked:
            self.checked = True
            try:
                lib = ctypes.CDLL(str(Path(__file__).with_name('libstorage.so')))
                lib.mv_storage_merge.argtypes = [ctypes.c_char_p] * 3 + [ctypes.POINTER(Source), ctypes.c_size_t,
                    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int,
                    ctypes.POINTER(Segment), ctypes.c_char_p, ctypes.c_size_t]
                lib.mv_storage_merge.restype = ctypes.c_int
                if hasattr(lib, 'mv_vector_parse'):
                    lib.mv_vector_parse.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_float)]
                    lib.mv_vector_parse.restype = ctypes.c_int
                if hasattr(lib, 'mv_insert_prepare'):
                    lib.mv_insert_prepare.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(Column), ctypes.c_size_t, ctypes.c_size_t]
                    lib.mv_insert_prepare.restype = ctypes.c_void_p
                    lib.mv_insert_free.argtypes = [ctypes.c_void_p]
                    lib.mv_insert_free.restype = None
                    lib.mv_insert_count.argtypes = [ctypes.c_void_p]
                    lib.mv_insert_count.restype = ctypes.c_size_t
                    lib.mv_insert_data.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
                    lib.mv_insert_data.restype = ctypes.POINTER(ctypes.c_ubyte)
                    lib.mv_insert_ids.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64), ctypes.c_size_t]
                    lib.mv_insert_ids.restype = ctypes.c_int
                self.lib = lib
            except (OSError, AttributeError): pass
        return self.lib is not None

    def prepare_insert(self, text, tables):
        if os.environ.get('MINIDB_CPP_INSERT') == '0': return None
        head = _INSERT_TABLE.match(text)
        if head is None: return None
        name = head[1].casefold()
        table = tables.get(name)
        if table is None or len(table['cols']) > 256: return None
        parts = table.get('segments')
        if parts is None or len(parts) > 16 or not all(p.get('columnar') for p in parts): return None
        vectors = [c for c in table['cols'] if c['type'] == 'VECTOR']
        if len(vectors) != 1 or not 1 <= vectors[0]['n'] <= 4096: return None
        if not self.available() or not hasattr(self.lib, 'mv_insert_prepare'): return None
        descriptors = (Column * len(table['cols']))()
        names = []
        for i, col in enumerate(table['cols']):
            if not col['name'].isascii() or '\0' in col['name']: return None
            width = col.get('n', 0)
            if not 0 <= width <= 0xffffffff: return None
            encoded = col['name'].encode('ascii'); names.append(encoded)
            descriptors[i] = Column(encoded, {'BIGINT':0,'VARCHAR':1,'VECTOR':2}[col['type']], width)
        try: data = text[head.end():].encode('utf-8')
        except UnicodeError: return None
        handle = self.lib.mv_insert_prepare(data, len(data), descriptors, len(descriptors), table['pk'])
        if not handle: return None
        batch = PreparedInsert(self.lib, handle)
        self.prepared_inserts += 1
        return name, batch

    def parse_vector(self, text, dimension):
        if (os.environ.get('MINIDB_CPP_VECTOR') == '0' or not 1 <= dimension <= 4096 or
                len(text) > 65536 or not text.isascii() or not self.available() or
                not hasattr(self.lib, 'mv_vector_parse')): return None
        data = text.encode('ascii')
        result = array('f', [0.]) * dimension
        output = (ctypes.c_float * dimension).from_buffer(result)
        if self.lib.mv_vector_parse(data, len(data), dimension, output): return None
        self.parsed_vectors += 1
        return result

    def merge(self, sources, payload, dimension, row_path, vector_path, key_path, bounded):
        descriptors = (Source * len(sources))()
        owners = []
        for i, (rows, vectors, count, excluded) in enumerate(sources):
            ids = (ctypes.c_int64 * len(excluded))(*excluded)
            rows, vectors = os.fsencode(rows), os.fsencode(vectors)
            owners.extend((ids, rows, vectors))
            descriptors[i] = Source(rows, vectors, count, ids, len(excluded))
        data = (ctypes.c_ubyte * len(payload)).from_buffer(payload)
        result = Segment(); message = ctypes.create_string_buffer(512)
        status = self.lib.mv_storage_merge(os.fsencode(row_path), os.fsencode(vector_path), os.fsencode(key_path),
            descriptors, len(sources), data, len(payload), dimension, int(bounded), ctypes.byref(result), message, len(message))
        if status: raise OSError(message.value.decode('utf-8', 'replace'))
        self.merges += 1
        return result


vector_parser = NativeStorage()
