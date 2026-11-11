"""Bounded immutable-index mappings; large files retain buffered scans."""
from collections import OrderedDict
from contextlib import contextmanager
import ctypes
import mmap
import os

MAP_BYTES = 8 << 20
MAP_FILES = 16


class MappedIndex:
    def __init__(self, stream):
        # Kernels borrow a writable address but only read it. MAP_PRIVATE
        # leaves the persistent file untouched and pages remain demand loaded.
        self.mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_COPY)
        try:
            self.owner = ctypes.c_char.from_buffer(self.mapping)
            self.address = ctypes.addressof(self.owner)
        except BaseException:
            self.owner = None
            self.mapping.close()
            raise
        self.cost = max(4096, len(self.mapping))

    def close(self):
        self.owner = None
        self.mapping.close()

    def blocks(self, block_bytes):
        for offset in range(8, len(self.mapping), block_bytes):
            view = memoryview(self.mapping)[offset:offset + block_bytes]
            try:
                yield view, ctypes.c_char_p(self.address + offset)
            finally:
                view.release()


class IndexReader:
    def __init__(self):
        self.maps = OrderedDict()
        self.bytes = 0

    def clear(self):
        while self.maps:
            _, node = self.maps.popitem(last=False)
            node.close()
        self.bytes = 0

    @contextmanager
    def blocks(self, path, block_bytes, magic, mapped=False):
        enabled = mapped and os.environ.get('MINIDB_INDEX_MMAP') != '0'
        key = str(path)
        node = self.maps.get(key) if enabled else None
        if node is not None:
            self.maps.move_to_end(key)
        else:
            with open(path, 'rb') as stream:
                if stream.read(8) != magic: raise OSError('invalid index header')
                cost = max(4096, os.fstat(stream.fileno()).st_size)
                if enabled and cost <= MAP_BYTES:
                    while self.maps and (self.bytes + cost > MAP_BYTES or len(self.maps) >= MAP_FILES):
                        _, old = self.maps.popitem(last=False)
                        self.bytes -= old.cost
                        old.close()
                    try:
                        node = MappedIndex(stream)
                    except (OSError, ValueError, OverflowError, MemoryError):
                        node = None
                    if node is not None:
                        self.maps[key] = node
                        self.bytes += node.cost
                if node is None:
                    yield ((data, None) for data in iter(lambda: stream.read(block_bytes), b''))
                    return
        blocks = node.blocks(block_bytes)
        try:
            yield blocks
        finally:
            blocks.close()
