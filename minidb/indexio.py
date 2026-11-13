"""Bounded immutable-index mappings; large files retain buffered scans."""
from collections import OrderedDict
from contextlib import contextmanager
import ctypes
import mmap
import os
from pathlib import Path

MAP_BYTES = 8 << 20
MAP_FILES = 16


def file_cache_budget():
    """Conservative visible cgroup-v2 headroom; None selects bounded streaming.

    Docker's private cgroup namespace normally exposes the group at '/'.
    With a host namespace, include visible ancestors and their aggregate usage.
    Unsupported mounts, missing counters, v1 or malformed values fail closed.
    This is a per-table planning estimate, not a memory reservation.
    """
    root = Path('/sys/fs/cgroup')
    try:
        memberships = [line[3:] for line in Path('/proc/self/cgroup').read_text().splitlines()
                       if line.startswith('0::')]
        if len(memberships) != 1:
            return None
        relative = Path(memberships[0])
        if not relative.is_absolute() or '..' in relative.parts:
            return None
        group = root / str(relative).lstrip('/')
        budgets = []
        while True:
            # The host hierarchy root can lack the memory controller files.
            if group == root and group != root / str(relative).lstrip('/') and not (group / 'memory.max').exists():
                break
            limits = []
            for name in ('memory.max', 'memory.high'):
                raw = (group / name).read_text().strip()
                if raw != 'max':
                    value = int(raw)
                    if value < 0: return None
                    limits.append(value)
            if limits:
                stats = dict(line.split() for line in (group / 'memory.stat').read_text().splitlines())
                anon, kernel = int(stats['anon']), int(stats['kernel'])
                if anon < 0 or kernel < 0: return None
                budgets.append(max(0, min(limits) - anon - kernel - (1 << 20)))
            if group == root:
                break
            group = group.parent
        return min(budgets) if budgets else None
    except (OSError, ValueError, KeyError):
        return None


def read_range(stream, block_bytes, remaining):
    while remaining:
        data = stream.read(min(block_bytes, remaining))
        if not data: raise OSError('incomplete index range')
        remaining -= len(data)
        yield data, None


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

    def blocks(self, block_bytes, start=8, stop=None):
        stop = len(self.mapping) if stop is None else stop
        for offset in range(start, stop, block_bytes):
            view = memoryview(self.mapping)[offset:min(offset + block_bytes, stop)]
            try:
                yield view, ctypes.c_char_p(self.address + offset)
            finally:
                view.release()


class IndexReader:
    def __init__(self):
        self.maps = OrderedDict()
        self.bytes = 0
        self.profile_table = None
        self.streaming = False
        self.cache_budget = None

    def block_size(self, table, segments, record_bytes):
        if self.profile_table is not table:
            budget = file_cache_budget()
            self.cache_budget = budget
            footprint = sum(path.stat().st_size for path, _ in segments)
            # Unknown/unlimited cgroups do not establish a safe cache budget.
            self.streaming = budget is None or footprint > budget
            self.profile_table = table
        target = (256 << 10) if self.streaming else (4 << 20)
        return record_bytes * max(1, target // record_bytes)

    def clear(self):
        while self.maps:
            _, node = self.maps.popitem(last=False)
            node.close()
        self.bytes = 0
        self.profile_table = None
        self.streaming = False
        self.cache_budget = None

    @contextmanager
    def blocks(self, path, block_bytes, magic, mapped=False, start=8, stop=None):
        enabled = mapped and os.environ.get('MINIDB_INDEX_MMAP') != '0'
        key = str(path)
        node = self.maps.get(key) if enabled else None
        if node is not None and self.streaming and node.cost > block_bytes + 8:
            self.maps.pop(key)
            self.bytes -= node.cost
            node.close()
            node = None
        if node is not None:
            self.maps.move_to_end(key)
        else:
            with open(path, 'rb') as stream:
                if self.streaming:
                    try: os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
                    except (AttributeError, OSError): pass
                if stream.read(8) != magic: raise OSError('invalid index header')
                cost = max(4096, os.fstat(stream.fileno()).st_size)
                if enabled and cost <= MAP_BYTES and (not self.streaming or cost <= block_bytes + 8):
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
                    if start != 8: stream.seek(start)
                    blocks = (read_range(stream, block_bytes, stop - start) if stop is not None else
                              ((data, None) for data in iter(lambda: stream.read(block_bytes), b'')))
                    try:
                        yield blocks
                    finally:
                        blocks.close()
                    return
        blocks = node.blocks(block_bytes, start, stop)
        try:
            yield blocks
        finally:
            blocks.close()
