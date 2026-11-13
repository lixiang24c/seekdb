"""Variable-sized immutable row files, atomically published by one manifest."""
import copy
import bisect
from array import array
import heapq
import itertools
import json
import math
import os
import struct
import uuid
from contextlib import ExitStack
from pathlib import Path
from .native_storage import NativeStorage

MAGIC = b'MVROW001'
INDEX_MAGIC = b'MVFLT001'
KEY_MAGIC = b'MVKEY001'
OFFSET_MAGIC = b'MVKEY002'
KEY_OFFSET = struct.Struct('<qQ')
U32 = struct.Struct('<I')
I64 = struct.Struct('<q')
SMALL_SEGMENT, MAX_SEGMENTS, MAX_EXCLUDED = 64, 32, 128
MERGE_FANOUT = 4


def strict_json(text):
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError('duplicate JSON key')
            result[k] = v
        return result
    def invalid(value):
        raise ValueError('nonfinite JSON value: ' + value)
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            return invalid(value)
        return number
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)


class Codec:
    def __init__(self, cols):
        self.cols = cols
        self.formats = [struct.Struct('<' + str(c['n']) + 'f') if c['type'] == 'VECTOR' else None for c in cols]

    def encode(self, row):
        chunks = []
        for c, fmt, value in zip(self.cols, self.formats, row):
            if c['type'] == 'BIGINT':
                chunks.append(I64.pack(value))
            elif c['type'] == 'VARCHAR':
                raw = value.encode('utf-8')
                chunks.extend((U32.pack(len(raw)), raw))
            else:
                chunks.append(fmt.pack(*value))
        return b''.join(chunks)

    def decode(self, data):
        row, pos = [], 0
        for c, fmt in zip(self.cols, self.formats):
            if c['type'] == 'BIGINT':
                row.append(I64.unpack_from(data, pos)[0]); pos += 8
            elif c['type'] == 'VARCHAR':
                size = U32.unpack_from(data, pos)[0]; pos += 4
                row.append(data[pos:pos + size].decode('utf-8')); pos += size
            else:
                row.append(list(fmt.unpack_from(data, pos))); pos += fmt.size
        if pos != len(data):
            raise OSError('invalid row length')
        return row

    def decode_fields(self, data, fields):
        row, pos = [None] * len(self.cols), 0
        for i, (col, fmt) in enumerate(zip(self.cols, self.formats)):
            kind = col['type']
            size = 8 if kind == 'BIGINT' else fmt.size if fmt else 4 + U32.unpack_from(data, pos)[0]
            if i in fields:
                if kind == 'BIGINT': row[i] = I64.unpack_from(data, pos)[0]
                elif kind == 'VARCHAR': row[i] = data[pos + 4:pos + size].decode('utf-8')
                else: row[i] = list(fmt.unpack_from(data, pos))
            pos += size
        if pos != len(data): raise OSError('invalid row length')
        return row

    def field(self, data, target):
        """Locate a binary field without decoding vectors or copying strings."""
        pos = 0
        for i, (col, fmt) in enumerate(zip(self.cols, self.formats)):
            size = 8 if col['type'] == 'BIGINT' else fmt.size if fmt else 4 + U32.unpack_from(data, pos)[0]
            if i == target:
                return pos, size
            pos += size
        raise OSError('invalid field position')


def raw_rows(path, codec, pk, count=None, bounded_read=False):
    with open(path, 'rb', buffering=32768 if bounded_read else -1) as f:
        if bounded_read:
            try: os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
            except (AttributeError, OSError): pass
        if f.read(8) != MAGIC:
            raise OSError('invalid row file header')
        seen = 0
        while count is None or seen < count:
            head = f.read(4)
            if not head: break
            seen += 1
            if len(head) != 4:
                raise OSError('incomplete row header')
            size = U32.unpack(head)[0]
            raw = f.read(size)
            if len(raw) != size:
                raise OSError('incomplete row')
            offset, _ = codec.field(raw, pk)
            yield I64.unpack_from(raw, offset)[0], raw


def read_rows(path, codec):
    with open(path, 'rb') as f:
        if f.read(8) != MAGIC:
            raise OSError('invalid row file header')
        while raw := f.read(4):
            if len(raw) != 4:
                raise OSError('incomplete row header')
            size = U32.unpack(raw)[0]
            data = f.read(size)
            if len(data) != size:
                raise OSError('incomplete row')
            yield codec.decode(data)


def write_rows(path, codec, rows, durable=False):
    count = 0
    with open(path, 'xb') as f:
        f.write(MAGIC)
        for row in rows:
            raw = codec.encode(row)
            f.write(U32.pack(len(raw))); f.write(raw); count += 1
        if durable:
            f.flush(); os.fsync(f.fileno())
    return count


class Storage:
    def __init__(self, directory):
        self.cpp = NativeStorage()
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.dirfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.manifest = self.root / 'manifest.json'
        self.in_txn = False
        self._path_cache = {}
        self._segment_cache = (None, None)
        try:
            with open(self.manifest, encoding='utf-8') as f:
                doc = strict_json(f.read())
            if doc.get('format') not in (1, 2) or not isinstance(doc.get('tables'), dict):
                raise OSError('unsupported or invalid manifest')
            self.tables = doc['tables']
            for t in self.tables.values():
                for part in self.segments(t):
                    for key, magic in [('file', MAGIC), ('index_file', INDEX_MAGIC), ('keys', KEY_MAGIC)]:
                        if part.get(key):
                            with open(self.path(part[key]), 'rb') as f:
                                if key == 'keys': f.seek(part.get('key_offset', 0))
                                if f.read(8) not in ((KEY_MAGIC, OFFSET_MAGIC) if key == 'keys' else (magic,)):
                                    raise OSError('invalid committed file header')
        except FileNotFoundError:
            # A missing committed data file is an error; only an absent manifest means new DB.
            if self.manifest.exists():
                raise
            self.tables = {}
            self.publish()
        self.committed = copy.deepcopy(self.tables)
        self.collect()

    def path(self, name):
        if not isinstance(name, str):
            raise OSError('invalid manifest path')
        cached = self._path_cache.get(name)
        if cached is not None: return cached
        if Path(name).name != name or not name.startswith('mv-'):
            raise OSError('invalid manifest path')
        result = self.root / name
        if len(self._path_cache) >= 256: self._path_cache.clear()
        self._path_cache[name] = result
        return result

    def fresh(self, suffix='rows'):
        return self.root / ('mv-' + uuid.uuid4().hex + '.' + suffix)

    def publish(self):
        # Data files have been fsynced before this operation. Persist their names first.
        os.fsync(self.dirfd)
        temp = self.fresh('manifest')
        with open(temp, 'x', encoding='utf-8') as f:
            json.dump({'format': 2, 'tables': self.tables}, f, ensure_ascii=False, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.manifest)
        os.fsync(self.dirfd)
        self.committed = copy.deepcopy(self.tables)

    def collect(self):
        keep = {part[k] for state in (self.tables, self.committed) for t in state.values()
                for part in self.segments(t) for k in ('file', 'index_file', 'keys') if part.get(k)}
        for path in self.root.iterdir():
            if path.name.startswith('mv-') and path.is_file() and path.name not in keep:
                path.unlink()

    def begin(self):
        self.in_txn = True

    def commit(self):
        self.publish()
        self.in_txn = False
        self.collect()

    def rollback(self):
        self.tables = copy.deepcopy(self.committed)
        self.in_txn = False
        self.collect()

    def install(self, name, table):
        self.tables[name] = table
        if not self.in_txn:
            self.publish()
        self.collect()

    @staticmethod
    def segments(table):
        if 'segments' in table:
            return table['segments']
        # Read the original single-file format without changing its committed files.
        return [{k: table[k] for k in ('file', 'index_file', 'count') if k in table}] if table.get('file') else []

    def scalar_layout(self, table):
        fields = [i for i, c in enumerate(table['cols']) if c['type'] != 'VECTOR']
        return fields, Codec([table['cols'][i] for i in fields])

    def restore_vector(self, table, part, raw, position, vectors=None, codec=None):
        if not part.get('columnar'): return raw
        codec = codec or Codec(table['cols'])
        vc = next(i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR')
        offset, size = codec.field(raw, vc)
        stride = size + 8
        if vectors is None:
            with open(self.path(part['index_file']), 'rb') as f:
                data = os.pread(f.fileno(), stride, 8 + position * stride)
        else:
            data = vectors.read(stride)
        if len(data) != stride: raise OSError('incomplete vector column')
        return raw[:offset] + data[8:] + raw[offset:]

    def part_records(self, table, part):
        excluded = set(part.get('excluded', ()))
        fields, scalar = self.scalar_layout(table)
        codec = scalar if part.get('columnar') else Codec(table['cols'])
        pk = fields.index(table['pk']) if part.get('columnar') else table['pk']
        reader = raw_rows(self.path(part['file']), codec, pk, part['count'])
        full_codec = Codec(table['cols'])
        with ExitStack() as opened:
            vectors = opened.enter_context(open(self.path(part['index_file']), 'rb')) if part.get('columnar') else None
            if vectors and vectors.read(8) != INDEX_MAGIC: raise OSError('invalid vector column')
            try:
                for position, (ident, raw) in enumerate(reader):
                    raw = self.restore_vector(table, part, raw, position, vectors, full_codec)
                    if ident not in excluded: yield ident, raw
            finally: reader.close()

    def column_records(self, table, part, bounded_read=False):
        """Merge physical columns without constructing logical vector rows."""
        vc = next(i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR')
        if not part.get('columnar'):
            codec = Codec(table['cols'])
            for ident, raw in self.part_records(table, part):
                offset, size = codec.field(raw, vc)
                yield ident, raw[:offset] + raw[offset + size:], raw[offset:offset + size]
            return
        fields, codec = self.scalar_layout(table)
        pk = fields.index(table['pk'])
        excluded = set(part.get('excluded', ()))
        stride = 8 + table['cols'][vc]['n'] * 4
        reader = raw_rows(self.path(part['file']), codec, pk, part['count'], bounded_read=bounded_read)
        try:
            with open(self.path(part['index_file']), 'rb', buffering=32768 if bounded_read else -1) as vectors:
                if bounded_read:
                    try: os.posix_fadvise(vectors.fileno(), 0, 0, os.POSIX_FADV_RANDOM)
                    except (AttributeError, OSError): pass
                if vectors.read(8) != INDEX_MAGIC: raise OSError('invalid vector column')
                for ident, raw in reader:
                    vector = vectors.read(stride)
                    if len(vector) != stride or I64.unpack_from(vector)[0] != ident:
                        raise OSError('misaligned vector column')
                    if ident not in excluded: yield ident, raw, vector[8:]
        finally: reader.close()

    def incoming_columns(self, table, rows):
        fields, codec = self.scalar_layout(table)
        vc = next(i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR')
        vector = struct.Struct('<' + str(table['cols'][vc]['n']) + 'f')
        for row in rows:
            value = row[vc]
            packed = value.tobytes() if isinstance(value, array) and value.typecode == 'f' else vector.pack(*value)
            yield row[table['pk']], codec.encode([row[i] for i in fields]), packed

    def scalar_ids(self, table, match, columns, budget=4096, builder=None):
        """Evaluate predicates without touching the canonical vector column."""
        if not all(p.get('columnar') for p in self.segments(table)): return None
        fields, codec = self.scalar_layout(table)
        needed = {fields.index(i) for i in columns}
        pk = fields.index(table['pk'])
        ids = set()
        for part in self.segments(table):
            excluded = set(part.get('excluded', ()))
            for ident, raw in raw_rows(self.path(part['file']), codec, pk, part['count']):
                if ident in excluded: continue
                scalars = codec.decode_fields(raw, needed)
                row = [None] * len(table['cols'])
                for i in needed: row[fields[i]] = scalars[i]
                if builder is not None: builder.add(ident, row[next(iter(columns))])
                if match(row):
                    ids.add(ident)
                    if len(ids) > budget: return None
        return ids

    def index_segments(self, table):
        cached_table, cached = self._segment_cache
        if cached_table is table: return cached
        result = [(self.path(part['index_file']), set(part.get('excluded', ())))
                  for part in self.segments(table) if part.get('index_file')]
        self._segment_cache = (table, result)
        return result

    def index_paths(self, table):
        return [self.path(part['index_file']) for part in self.segments(table) if part.get('index_file')]

    def native_segment(self, table, parts, incoming, created, bounded=False):
        vc = next((i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR'), None)
        if (vc is None or not 1 <= table['cols'][vc]['n'] <= 4096 or len(parts) > 16 or
                not all(p.get('columnar') for p in parts) or not self.cpp.available()):
            return None
        payload = getattr(incoming, 'payload', None)
        if payload is None:
            payload = bytearray()
            for ident, scalar, vector in self.incoming_columns(table, incoming):
                payload.extend(I64.pack(ident)); payload.extend(U32.pack(len(scalar)))
                payload.extend(scalar); payload.extend(vector)
        path, vectors, keys = self.fresh(), self.fresh('index'), self.fresh('keys')
        sources = [(self.path(p['file']), self.path(p['index_file']), p['count'], sorted(p.get('excluded', ()))) for p in parts]
        result = self.cpp.merge(sources, payload, table['cols'][vc]['n'], path, vectors, keys, bounded)
        created.extend((path, vectors))
        return {'file': path.name, 'keys': path.name, 'index_file': vectors.name,
                'count': result.count, 'min': result.minimum if result.count else None,
                'max': result.maximum if result.count else None, 'key_offset': result.key_offset,
                'level': 0, 'columnar': True}

    def write_segment(self, table, records, created, columns=False):
        path = self.fresh()
        keys = self.fresh('keys')
        codec = Codec(table['cols'])
        vc = next((i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR'), None)
        idx = self.fresh('index') if vc is not None else None
        part = {'file': path.name, 'keys': keys.name, 'index_file': idx.name if idx else None,
                'count': 0, 'min': None, 'max': None, 'level': 0, 'columnar': vc is not None}
        opened = []
        try:
            f = open(path, 'xb'); opened.append(f); f.write(MAGIC)
            k = open(keys, 'x+b'); opened.append(k); k.write(OFFSET_MAGIC)
            v = None
            if idx:
                v = open(idx, 'xb'); opened.append(v); v.write(INDEX_MAGIC)
            for record in records:
                if columns:
                    ident, stored, vector = record
                else:
                    ident, raw = record
                    stored, vector = raw, None
                    if vc is not None:
                        offset, size = codec.field(raw, vc)
                        stored, vector = raw[:offset] + raw[offset + size:], raw[offset:offset + size]
                if part['count'] and ident <= part['max']:
                    raise OSError('records must have unique ascending primary keys')
                row_offset = f.tell()
                f.write(U32.pack(len(stored))); f.write(stored)
                packed_key = I64.pack(ident)
                k.write(KEY_OFFSET.pack(ident, row_offset))
                if v:
                    v.write(packed_key); v.write(vector)
                if not part['count']: part['min'] = ident
                part['max'] = ident
                part['count'] += 1
            if vc is not None:
                part['key_offset'] = f.tell()
                part['keys'] = path.name
                k.flush(); k.seek(0)
                while data := k.read(65536): f.write(data)
                k.close(); opened.remove(k); keys.unlink()
            for stream in opened:
                stream.flush(); os.fsync(stream.fileno())
            created.extend([path] + ([keys] if vc is None else []) + ([idx] if idx else []))
            return part
        except BaseException:
            path.unlink(missing_ok=True)
            keys.unlink(missing_ok=True)
            if idx: idx.unlink(missing_ok=True)
            raise
        finally:
            for stream in opened: stream.close()

    def rewrite(self, table, rows):
        new, created = copy.deepcopy(table), []
        codec = Codec(table['cols'])
        part = self.write_segment(table, ((row[table['pk']], codec.encode(row)) for row in rows), created)
        part['level'] = max(0, (part['count'] // 100).bit_length() - 1)
        new.update(segments=[part], count=part['count'])
        new.pop('file', None); new.pop('index_file', None)
        return new

    def insert(self, table, incoming):
        """Append one immutable run; merge runs geometrically, not on every insert."""
        from .sql import fail
        new, created = copy.deepcopy(table), []
        parts = copy.deepcopy([p for p in self.segments(table) if p['count']])
        codec, pk = Codec(table['cols']), table['pk']
        ids = incoming.ids if hasattr(incoming, 'ids') else [row[pk] for row in incoming]
        try:
            for part in parts:
                if not part.get('keys'):
                    # One-time primary key metadata upgrade for existing MVP databases.
                    path = self.fresh('keys'); created.append(path)
                    with open(path, 'xb') as f:
                        f.write(KEY_MAGIC)
                        first = last = None
                        for ident, _ in raw_rows(self.path(part['file']), codec, pk, part['count']):
                            if first is None: first = ident
                            last = ident; f.write(I64.pack(ident))
                        f.flush(); os.fsync(f.fileno())
                    part.update(keys=path.name, min=first, max=last,
                                level=max(0, (part['count'] // 100).bit_length() - 1))
                lo = bisect.bisect_left(ids, part['min'])
                hi = bisect.bisect_right(ids, part['max'])
                if lo == hi:
                    continue
                with open(self.path(part['keys']), 'rb') as f:
                    f.seek(part.get('key_offset', 0))
                    header = f.read(8)
                    if header not in (KEY_MAGIC, OFFSET_MAGIC): raise OSError('invalid key index header')
                    stride = 16 if header == OFFSET_MAGIC else 8
                    for ident in ids[lo:hi]:
                        if ident in part.get('excluded', ()): continue
                        left, right = 0, part['count']
                        while left < right:
                            mid = (left + right) // 2
                            raw = os.pread(f.fileno(), 8, part.get('key_offset', 0) + 8 + mid * stride)
                            if len(raw) != 8: raise OSError('incomplete key index')
                            found = I64.unpack(raw)[0]
                            if found == ident: fail('duplicate primary key', 'duplicate_key')
                            if found < ident: left = mid + 1
                            else: right = mid
            level = max(0, (len(incoming) // 100).bit_length() - 1)
            merging = []
            fanout = 2 if table.get('indexes') else MERGE_FANOUT
            while parts:
                group = []
                while parts and parts[-1]['level'] <= level and len(group) < fanout - 1:
                    group.append(parts.pop())
                if len(group) < fanout - 1:
                    parts.extend(reversed(group))
                    break
                merging.extend(group)
                level = max(level, max(p['level'] for p in group)) + 1
            if len(merging) <= 16:
                # Plan the existing merge cascade first; write only its final run.
                columns = any(c['type'] == 'VECTOR' for c in table['cols'])
                part = self.native_segment(table, merging, incoming, created)
                if part is None:
                    readers = [(self.column_records(table, p) if columns else self.part_records(table, p)) for p in merging]
                    try:
                        records = self.incoming_columns(table, incoming) if columns else ((row[pk], codec.encode(row)) for row in incoming)
                        if readers: records = heapq.merge(records, *readers, key=lambda item: item[0])
                        part = self.write_segment(table, records, created, columns=columns)
                    finally:
                        for reader in readers: reader.close()
            else:
                # Keep the old two-reader path for unusually wide overlay cascades.
                part = self.write_segment(table, ((row[pk], codec.encode(row)) for row in incoming), created)
                for old in merging:
                    readers = [self.part_records(table, p) for p in (old, part)]
                    try:
                        part = self.write_segment(table, heapq.merge(*readers, key=lambda item: item[0]), created)
                    finally:
                        for reader in readers: reader.close()
            part['level'] = level
            new.update(segments=parts + [part], count=table['count'] + len(incoming))
            new.pop('file', None); new.pop('index_file', None)
            return new
        except BaseException:
            for path in created: path.unlink(missing_ok=True)
            raise

    def lookup(self, table, ident):
        """Return a binary row and its segment; old key files remain readable."""
        codec = Codec(table['cols'])
        for part in self.segments(table):
            if not part['count'] or ident in part.get('excluded', ()): continue
            if part.get('min') is not None and not part['min'] <= ident <= part['max']: continue
            if part.get('keys'):
                with open(self.path(part['keys']), 'rb') as f:
                    f.seek(part.get('key_offset', 0))
                    header = f.read(8)
                    if header not in (KEY_MAGIC, OFFSET_MAGIC): raise OSError('invalid key index header')
                    stride = 16 if header == OFFSET_MAGIC else 8
                    left, right = 0, part['count']
                    found = None
                    while left < right:
                        mid = (left + right) // 2
                        data = os.pread(f.fileno(), stride, part.get('key_offset', 0) + 8 + mid * stride)
                        if len(data) != stride: raise OSError('incomplete key index')
                        key = I64.unpack_from(data)[0]
                        if key == ident:
                            found = data; break
                        if key < ident: left = mid + 1
                        else: right = mid
                    if found is None: continue
                    if header == OFFSET_MAGIC:
                        offset = KEY_OFFSET.unpack(found)[1]
                        with open(self.path(part['file']), 'rb') as rows:
                            rows.seek(offset)
                            head = rows.read(4)
                            if len(head) != 4: raise OSError('incomplete row header')
                            size = U32.unpack(head)[0]; raw = rows.read(size)
                            if len(raw) != size: raise OSError('incomplete row')
                            return part, self.restore_vector(table, part, raw, mid)
            for key, raw in self.part_records(table, part):
                if key == ident: return part, raw
                if key > ident: break
        return None, None

    def lookup_many(self, table, idents):
        """Read a bounded TopK ID set with one key/row handle per segment."""
        pending = set(idents)
        codec = Codec(table['cols'])
        for part in self.segments(table):
            if not pending: return
            if not part['count']: continue
            excluded = set(part.get('excluded', ()))
            wanted = sorted(i for i in pending if i not in excluded and
                            (part.get('min') is None or part['min'] <= i <= part['max']))
            if not wanted: continue
            with ExitStack() as opened:
                offsets = []
                if part.get('keys'):
                    keys = opened.enter_context(open(self.path(part['keys']), 'rb'))
                    keys.seek(part.get('key_offset', 0))
                    header = keys.read(8)
                    if header not in (KEY_MAGIC, OFFSET_MAGIC): raise OSError('invalid key index header')
                    if header == OFFSET_MAGIC:
                        lower = 0
                        for ident in wanted:
                            left, right = lower, part['count']
                            while left < right:
                                mid = (left + right) // 2
                                data = os.pread(keys.fileno(), 16, part.get('key_offset', 0) + 8 + mid * 16)
                                if len(data) != 16: raise OSError('incomplete key index')
                                key, offset = KEY_OFFSET.unpack(data)
                                if key == ident:
                                    offsets.append((ident, offset, mid)); lower = mid + 1
                                    break
                                if key < ident: left = mid + 1
                                else: right = mid
                            else: lower = left
                        if offsets:
                            rows = opened.enter_context(open(self.path(part['file']), 'rb'))
                            for ident, offset, position in offsets:
                                rows.seek(offset)
                                head = rows.read(4)
                                if len(head) != 4: raise OSError('incomplete row header')
                                raw = rows.read(U32.unpack(head)[0])
                                if len(raw) != U32.unpack(head)[0]: raise OSError('incomplete row')
                                pending.remove(ident)
                                yield ident, self.restore_vector(table, part, raw, position)
                        continue
                # Legacy key-only or unindexed files have no row offsets.
                wanted = set(wanted)
                reader = self.part_records(table, part)
                try:
                    for ident, raw in reader:
                        if ident in wanted:
                            pending.remove(ident); wanted.remove(ident)
                            yield ident, raw
                            if not wanted: break
                finally: reader.close()

    def add_index(self, table):
        new, created = copy.deepcopy(table), []
        codec = Codec(table['cols'])
        vc = next(i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR')
        parts = copy.deepcopy(self.segments(table))
        try:
            from .indexio import file_cache_budget
            budget = file_cache_budget()
            dim = table['cols'][vc]['n']
            code_cost = table['count'] * dim + dim * 2048 + 128
            footprint = sum(self.path(p['index_file']).stat().st_size for p in parts if p.get('index_file'))
            compact = (budget is not None and footprint > budget and
                       512 <= table['count'] <= 20000 and 16 <= dim <= 256 and
                       code_cost <= (2 << 20) and code_cost + dim * 2048 + table['count'] * 8 + (256 << 10) <= budget)
            if compact and 1 < len(parts) <= 16 and all(p.get('columnar') for p in parts):
                packed = self.native_segment(table, parts, (), created, bounded=True)
                if packed is None:
                    readers = [self.column_records(table, p, bounded_read=True) for p in parts]
                    try:
                        packed = self.write_segment(new, heapq.merge(*readers, key=lambda item: item[0]), created, columns=True)
                    finally:
                        for reader in readers: reader.close()
                packed['level'] = max(p.get('level', 0) for p in parts) + 1
                parts = [packed]
            for part in parts:
                if part.get('index_file'): continue
                path = self.fresh('index'); created.append(path)
                with open(path, 'xb') as f:
                    f.write(INDEX_MAGIC)
                    for ident, raw in self.part_records(table, part):
                        offset, size = codec.field(raw, vc)
                        f.write(I64.pack(ident)); f.write(memoryview(raw)[offset:offset + size])
                    f.flush(); os.fsync(f.fileno())
                part['index_file'] = path.name
            new.update(segments=parts)
            new.pop('file', None); new.pop('index_file', None)
            return new
        except BaseException:
            for path in created: path.unlink(missing_ok=True)
            raise

    def mutate(self, table, match, filter_columns, sets, delete):
        """Keep untouched segments and copy unchanged binary rows without decoding."""
        codec, created = Codec(table['cols']), []
        new, parts, count = copy.deepcopy(table), [], 0
        def replace(ident, raw):
            nonlocal count
            count += 1
            if not delete:
                row = codec.decode(raw)
                for i, value in sets: row[i] = value
                yield ident, codec.encode(row)
        try:
            for part in self.segments(table):
                reader = self.part_records(table, part)
                try:
                    for skipped, (ident, raw) in enumerate(reader):
                        if match(codec.decode_fields(raw, filter_columns)):
                            current = codec.decode_fields(raw, {i for i, _ in sets}) if not delete else None
                            if not delete and all(current[i] == value for i, value in sets):
                                count += 1
                                continue
                            break
                    else:
                        parts.append(copy.deepcopy(part)); continue
                    def changed():
                        prefix = self.part_records(table, part)
                        try: yield from itertools.islice(prefix, skipped)
                        finally: prefix.close()
                        yield from replace(ident, raw)
                        for key, data in reader:
                            if match(codec.decode_fields(data, filter_columns)):
                                yield from replace(key, data)
                            else: yield key, data
                    updated = self.write_segment(table, changed(), created)
                    updated['level'] = part.get('level', 0)
                    parts.append(updated)
                finally: reader.close()
            new.update(segments=parts, count=table['count'] - (count if delete else 0))
            new.pop('file', None); new.pop('index_file', None)
            return new, count
        except BaseException:
            for path in created: path.unlink(missing_ok=True)
            raise

    def merge_overlays(self, table):
        """Merge the trailing run of small segments; larger segments stay untouched."""
        parts = self.segments(table)
        start = len(parts)
        while start and parts[start - 1]['count'] < SMALL_SEGMENT: start -= 1
        tail = parts[start:]
        if len(tail) < 2 or start + 1 >= MAX_SEGMENTS: return None
        created = []
        merged = self.native_segment(table, tail, (), created)
        if merged is None:
            columns = any(c['type'] == 'VECTOR' for c in table['cols'])
            readers = [(self.column_records(table, p) if columns else self.part_records(table, p)) for p in tail]
            try:
                merged = self.write_segment(table, heapq.merge(*readers, key=lambda item: item[0]), created, columns=columns)
            finally:
                for reader in readers: reader.close()
        new = copy.deepcopy(table)
        new.update(segments=copy.deepcopy(parts[:start]) + ([merged] if merged['count'] else []))
        new.pop('file', None); new.pop('index_file', None)
        return new

    def replace_point(self, table, part, ident, replacement):
        # Small overlays merge cheaply; the whole table is rewritten only when masks pile up.
        parts = self.segments(table)
        if sum(len(p.get('excluded', ())) for p in parts) >= MAX_EXCLUDED:
            table = self.rewrite(table, self.rows(table))
            part, _ = self.lookup(table, ident)
        elif len(parts) >= MAX_SEGMENTS:
            table = self.merge_overlays(table) or self.rewrite(table, self.rows(table))
            part, _ = self.lookup(table, ident)
        new, created = copy.deepcopy(table), []
        parts = copy.deepcopy(self.segments(table))
        for current in parts:
            if current['file'] == part['file']:
                current['excluded'] = sorted(set(current.get('excluded', ())) | {ident})
        parts = [p for p in parts if len(p.get('excluded', ())) < p['count']]
        if replacement is not None:
            codec = Codec(table['cols'])
            delta = self.write_segment(table, [(ident, codec.encode(replacement))], created)
            parts.append(delta)
        new.update(segments=parts, count=table['count'] - int(replacement is None))
        new.pop('file', None); new.pop('index_file', None)
        return new

    def has_offsets(self, table):
        for part in self.segments(table):
            if not part['count']: continue
            if not part.get('keys'): return False
            with open(self.path(part['keys']), 'rb') as f:
                f.seek(part.get('key_offset', 0))
                if f.read(8) != OFFSET_MAGIC: return False
        return True

    def range_records(self, table, lower, upper, reverse=False):
        """Inclusive primary-key range, bounded memory and one cursor per segment."""
        codec = Codec(table['cols'])
        def segment(part):
            if not part['count'] or lower > upper: return
            if part.get('min') is not None and (part['max'] < lower or part['min'] > upper): return
            if part.get('keys'):
                with open(self.path(part['keys']), 'rb') as keys:
                    keys.seek(part.get('key_offset', 0))
                    header = keys.read(8)
                    if header == OFFSET_MAGIC:
                        def entry(i):
                            data = os.pread(keys.fileno(), 16, part.get('key_offset', 0) + 8 + i * 16)
                            if len(data) != 16: raise OSError('incomplete key index')
                            return KEY_OFFSET.unpack(data)
                        def lower_bound(value):
                            lo, hi = 0, part['count']
                            while lo < hi:
                                mid = (lo + hi) // 2
                                if entry(mid)[0] < value: lo = mid + 1
                                else: hi = mid
                            return lo
                        start, stop = lower_bound(lower), lower_bound(upper + 1)
                        if start == stop: return
                        with open(self.path(part['file']), 'rb') as rows:
                            if not reverse: rows.seek(entry(start)[1])
                            for i in (range(stop - 1, start - 1, -1) if reverse else range(start, stop)):
                                if reverse: rows.seek(entry(i)[1])
                                head = rows.read(4)
                                if len(head) != 4: raise OSError('incomplete row header')
                                size = U32.unpack(head)[0]; raw = rows.read(size)
                                if len(raw) != size: raise OSError('incomplete row')
                                raw = self.restore_vector(table, part, raw, i)
                                offset, _ = codec.field(raw, table['pk'])
                                ident = I64.unpack_from(raw, offset)[0]
                                if ident not in part.get('excluded', ()): yield ident, raw
                        return
                    if header != KEY_MAGIC: raise OSError('invalid key index header')
            if reverse: raise OSError('reverse scan requires row offsets')
            for ident, raw in self.part_records(table, part):
                if ident > upper: break
                if ident >= lower: yield ident, raw
        readers = [segment(part) for part in self.segments(table)]
        try:
            yield from heapq.merge(*readers, key=lambda item: item[0], reverse=reverse)
        finally:
            for reader in readers: reader.close()

    def records(self, table, ordered=True):
        codec = Codec(table['cols'])
        readers = [self.part_records(table, part) for part in self.segments(table)]
        try:
            if ordered:
                yield from heapq.merge(*readers, key=lambda item: item[0])
            else:
                yield from itertools.chain.from_iterable(readers)
        finally:
            for reader in readers: reader.close()

    def rows(self, table, columns=None, predicate=None, predicate_columns=None, bounds=None, reverse=False, ordered=True):
        if (bounds is None and columns is not None and
                all(table['cols'][i]['type'] != 'VECTOR' for i in columns) and
                all(p.get('columnar') for p in self.segments(table))):
            fields, scalar = self.scalar_layout(table)
            pk = fields.index(table['pk'])
            wanted = set(columns) | set(predicate_columns or ())
            selected = {fields.index(i) for i in wanted}
            def scan(part):
                for ident, raw in raw_rows(self.path(part['file']), scalar, pk, part['count']):
                    if ident in part.get('excluded', ()): continue
                    values = scalar.decode_fields(raw, selected)
                    row = [None] * len(table['cols'])
                    for i in selected: row[fields[i]] = values[i]
                    if predicate is None or predicate(row): yield ident, row
            readers = [scan(p) for p in self.segments(table)]
            try:
                source = heapq.merge(*readers, key=lambda item: item[0]) if ordered else itertools.chain.from_iterable(readers)
                for _, row in source: yield row
            finally:
                for reader in readers: reader.close()
            return
        codec = Codec(table['cols'])
        records = self.range_records(table, *bounds, reverse=reverse) if bounds is not None else self.records(table, ordered=ordered)
        try:
            for _, raw in records:
                if predicate is not None and not predicate(codec.decode_fields(raw, predicate_columns)):
                    continue
                yield codec.decode(raw) if columns is None else codec.decode_fields(raw, columns)
        finally:
            records.close()

    def close(self):
        try:
            self.rollback()
        finally:
            os.close(self.dirfd)
