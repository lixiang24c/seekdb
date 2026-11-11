"""Variable-sized immutable row files, atomically published by one manifest."""
import copy
import json
import math
import os
import struct
import uuid
from pathlib import Path

MAGIC = b'MVROW001'
INDEX_MAGIC = b'MVFLT001'
U32 = struct.Struct('<I')
I64 = struct.Struct('<q')


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
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.dirfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.manifest = self.root / 'manifest.json'
        self.in_txn = False
        try:
            with open(self.manifest, encoding='utf-8') as f:
                doc = strict_json(f.read())
            if doc.get('format') != 1 or not isinstance(doc.get('tables'), dict):
                raise OSError('unsupported or invalid manifest')
            self.tables = doc['tables']
            for t in self.tables.values():
                for key, magic in [('file', MAGIC), ('index_file', INDEX_MAGIC)]:
                    if t.get(key):
                        with open(self.path(t[key]), 'rb') as f:
                            if f.read(8) != magic:
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
        if not isinstance(name, str) or Path(name).name != name or not name.startswith('mv-'):
            raise OSError('invalid manifest path')
        return self.root / name

    def fresh(self, suffix='rows'):
        return self.root / ('mv-' + uuid.uuid4().hex + '.' + suffix)

    def publish(self):
        # Data files have been fsynced before this operation. Persist their names first.
        os.fsync(self.dirfd)
        temp = self.fresh('manifest')
        with open(temp, 'x', encoding='utf-8') as f:
            json.dump({'format': 1, 'tables': self.tables}, f, ensure_ascii=False, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(temp, self.manifest)
        os.fsync(self.dirfd)
        self.committed = copy.deepcopy(self.tables)

    def collect(self):
        keep = {t[k] for state in (self.tables, self.committed) for t in state.values()
                for k in ('file', 'index_file') if t.get(k)}
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

    def rewrite(self, table, rows):
        new = copy.deepcopy(table)
        path = self.fresh()
        idx = self.fresh('index') if table.get('indexes') else None
        codec = Codec(table['cols'])
        try:
            new['count'] = write_rows(path, codec, rows, durable=True)
            new['file'] = path.name
            new['index_file'] = None
            if idx:
                vc = next(i for i, c in enumerate(table['cols']) if c['type'] == 'VECTOR')
                fmt = codec.formats[vc]
                with open(idx, 'xb') as f:
                    f.write(INDEX_MAGIC)
                    for row in read_rows(path, codec):
                        f.write(I64.pack(row[table['pk']]))
                        f.write(fmt.pack(*row[vc]))
                    f.flush(); os.fsync(f.fileno())
                new['index_file'] = idx.name
            return new
        except BaseException:
            path.unlink(missing_ok=True)
            if idx:
                idx.unlink(missing_ok=True)
            raise

    def rows(self, table):
        return read_rows(self.path(table['file']), Codec(table['cols']))

    def close(self):
        try:
            self.rollback()
        finally:
            os.close(self.dirfd)
