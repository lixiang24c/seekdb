"""Bounded, optional immutable-segment VA cache; original vectors stay authoritative."""
from collections import OrderedDict
import ctypes
import os
from . import native

BUDGET = 2 << 20


def random_reads(stream):
    # Sparse candidate reads and bounded code-building passes should not trigger
    # readahead larger than the remaining cgroup file-cache allowance.
    try: os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
    except (AttributeError, OSError): pass


class ApproxCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0
        self.lib = None
        self.builds = self.hits = self.fallbacks = self.read_bytes = 0

    def clear(self):
        if self.lib:
            for handle, _, _ in self.entries.values():
                if handle: self.lib.mv_va_free(handle)
        self.entries.clear()
        self.bytes = 0

    def positions(self, path, screen, cached, k, cache_budget=None, streaming=False):
        mode = os.environ.get('MINIDB_VA', 'auto')
        if screen.query is None or mode == '0': return None
        if mode != '1' and (not streaming or cache_budget is None): return None
        if self.lib is None:
            if not native.available() or not hasattr(native._library, 'mv_va_build'): return None
            self.lib = native._library
            self.lib.mv_va_build.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
            self.lib.mv_va_build.restype = ctypes.c_void_p
            self.lib.mv_va_free.argtypes = [ctypes.c_void_p]
            self.lib.mv_va_free.restype = None
            self.lib.mv_va_select.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_double), ctypes.c_char_p,
                ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t]
            self.lib.mv_va_select.restype = ctypes.c_size_t
        key = str(path)
        entry = self.entries.get(key)
        if entry is None:
            count = (path.stat().st_size - 8) // (8 + screen.dimension * 4)
            cost = count * screen.dimension + screen.dimension * 2048 + 128
            if cost > BUDGET or count < 512 or count > 20000 or not 16 <= screen.dimension <= 256:
                return None
            # Admission, not cyclic eviction: repeated queries must not rebuild
            # every segment when the table's codes exceed the cache budget.
            limit = BUDGET
            if mode != '1':
                scratch = screen.dimension * 2048 + count * 8 + (256 << 10)
                limit = min(limit, max(0, cache_budget - scratch))
            if self.bytes + cost > limit or len(self.entries) >= 16: return None
            with open(path, 'rb') as stream:
                random_reads(stream)
                handle = self.lib.mv_va_build(stream.fileno(), screen.dimension, BUDGET)
            cost = cost if handle else 0
            self.entries[key] = (handle, cost, 0)
            self.bytes += cost
            self.builds += bool(handle)
            entry = self.entries[key]
        self.entries.move_to_end(key)
        handle, cost, failures = entry
        if not handle: return None
        mask, first = cached if isinstance(cached, tuple) else (cached, 0)
        output = (ctypes.c_uint32 * 1024)()
        with open(path, 'rb', buffering=0) as stream:
            random_reads(stream)
            count = self.lib.mv_va_select(handle, stream.fileno(), screen.query, mask, first,
                                          len(mask) if mask is not None else 0, k, output, 1024)
        if count == ctypes.c_size_t(-1).value:
            self.fallbacks += 1
            if failures >= 2:
                self.lib.mv_va_free(handle)
                self.bytes -= cost
                self.entries[key] = (None, 0, 0)
            else:
                self.entries[key] = (handle, cost, failures + 1)
            return None
        self.hits += 1
        return output[:count]

    def rows(self, path, positions, stride):
        """Coalesce candidate pages, retaining no more than one range at a time."""
        if not positions: return
        size = path.stat().st_size
        with open(path, 'rb', buffering=0) as stream:
            random_reads(stream)
            first = 0
            while first < len(positions):
                offset = 8 + positions[first] * stride
                start = offset // 4096 * 4096
                stop = (offset + stride + 4095) // 4096 * 4096
                last = first + 1
                while last < len(positions):
                    offset = 8 + positions[last] * stride
                    if offset // 4096 * 4096 > stop: break
                    stop = max(stop, (offset + stride + 4095) // 4096 * 4096)
                    last += 1
                stop = min(stop, size)
                stream.seek(start)
                data = bytearray(stop - start)
                view = memoryview(data)
                done = 0
                while done < len(data):
                    got = stream.readinto(view[done:])
                    if not got: raise OSError('incomplete VA candidate range')
                    done += got
                self.read_bytes += len(data)
                view.release()
                for pos in positions[first:last]:
                    offset = 8 + pos * stride - start
                    yield bytes(data[offset:offset + stride])
                first = last
