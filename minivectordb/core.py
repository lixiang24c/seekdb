"""Disk-backed reference engine. Immutable generations, atomic manifest commit.

This correctness-first implementation rewrites affected tables; it is not yet the
performance implementation. All database and temporary files live under data_dir.
"""
import heapq
import json
import math
import os
import struct
import uuid
from pathlib import Path


class DBError(Exception):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class Database:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / 'manifest.json'
        self.committed = json.loads(self.manifest.read_text()) if self.manifest.exists() else {}
        self.pending = None
        self.failed = False
        self._collect()
        import shutil
        for abandoned in self.root.glob('sort-*'):
            if abandoned.is_dir():
                shutil.rmtree(abandoned)

    def _collect(self):
        live = {v['file'] for v in self.committed.values()}
        live.update(v['index']['file'] for v in self.committed.values() if 'index' in v)
        live.update(v['index']['metadata'] for v in self.committed.values() if 'index' in v)
        live.update(v['keys'] for v in self.committed.values())
        live.update(f for v in self.committed.values() for f in v.get('segments', []))
        if self.pending is not None:
            live.update(v['file'] for v in self.pending.values())
            live.update(v['index']['file'] for v in self.pending.values() if 'index' in v)
            live.update(v['index']['metadata'] for v in self.pending.values() if 'index' in v)
            live.update(v['keys'] for v in self.pending.values())
            live.update(f for v in self.pending.values() for f in v.get('segments', []))
        for p in list(self.root.glob('table-*.jsonl')) + list(self.root.glob('index-*.bin')) + list(self.root.glob('keys-*.bin')) + list(self.root.glob('index-*.meta')):
            if p.name not in live:
                p.unlink()
        for p in self.root.glob('manifest-*.tmp'):
            p.unlink()

    @property
    def catalog(self):
        if self.failed:
            raise DBError('I/O failure: reopen database before continuing')
        return self.pending if self.pending is not None else self.committed

    def begin(self):
        if self.pending is not None:
            raise DBError('transaction already active')
        self.pending = dict(self.catalog)

    def rollback(self):
        if self.pending is None:
            raise DBError('no transaction')
        self.pending = None
        self._collect()

    def commit(self):
        if self.pending is None:
            raise DBError('no transaction')
        # No success is reported until both file contents and directory entry persist.
        temp = self.root / ('manifest-' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as out:
                out.write(encode(self.pending))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.manifest)
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            self.failed = True  # Publication may have succeeded; do not delete its files.
            raise
        self.committed = self.pending
        self.pending = None
        self._collect()

    def close(self):
        if not self.failed and self.pending is not None:
            self.rollback()

    def _statement(self, action):
        automatic = self.pending is None
        if automatic:
            self.begin()
        before = dict(self.catalog)
        try:
            result = action()
            if automatic:
                self.commit()
            return result
        except Exception:
            if not self.failed:
                self.pending = None if automatic else before
                self._collect()
            raise

    def _newfile(self):
        return self.root / ('table-' + uuid.uuid4().hex + '.jsonl')

    def _write(self, rows):
        path = self._newfile()
        with path.open('x', encoding='utf-8') as out:
            for row in rows:
                out.write(encode(row) + '\n')
            out.flush()
            os.fsync(out.fileno())
        return path.name

    def create(self, name, columns, primary):
        """columns: {name: ['BIGINT'|'VARCHAR'|'VECTOR', optional size]}."""
        def action():
            if name in self.catalog or not columns or primary not in columns:
                raise DBError('invalid or existing table')
            if columns[primary] != ['BIGINT']:
                raise DBError('primary key must be BIGINT')
            vectors = 0
            for spec in columns.values():
                if spec == ['BIGINT']:
                    continue
                if len(spec) != 2 or spec[0] not in ('VARCHAR', 'VECTOR') or type(spec[1]) is not int or spec[1] <= 0:
                    raise DBError('invalid type')
                vectors += spec[0] == 'VECTOR'
            if vectors > 1:
                raise DBError('only one vector column allowed')
            # Deep copy prevents caller mutation of the committed schema.
            schema = json.loads(encode(columns))
            self.pending[name] = {'columns': schema, 'primary': primary, 'file': self._write([]), 'keys': self._keyfile([])}
        return self._statement(action)

    def _table(self, name):
        if name not in self.catalog:
            raise DBError('unknown table')
        return self.catalog[name]

    def rows(self, name):
        table = self._table(name)
        yield from self._rows_of(table)

    def _rows_of(self, table):
        for filename in [table['file']] + table.get('segments', []):
            with (self.root / filename).open(encoding='utf-8') as src:
                for line in src:
                    yield json.loads(line)

    def _validate(self, table, row):
        if set(row) != set(table['columns']):
            raise DBError('column mismatch')
        result = {}
        for col, spec in table['columns'].items():
            value = row[col]
            if spec[0] == 'BIGINT':
                if type(value) is not int or not -(1 << 63) <= value < (1 << 63):
                    raise DBError('invalid BIGINT')
            elif spec[0] == 'VARCHAR':
                if not isinstance(value, str) or len(value) > spec[1]:
                    raise DBError('invalid VARCHAR')
                try:
                    value.encode('utf-8')
                except UnicodeError as e:
                    raise DBError('invalid Unicode') from e
            else:
                value = self.vector(value, spec[1])
            result[col] = value
        return result

    @staticmethod
    def vector(value, dimension):
        if not isinstance(value, (list, tuple)) or len(value) != dimension:
            raise DBError('vector dimension mismatch')
        result = []
        for number in value:
            try:
                if type(number) not in (float, int):
                    raise ValueError()
                f = struct.unpack('<f', struct.pack('<f', number))[0]
                if not math.isfinite(f):
                    raise ValueError()
            except (OverflowError, ValueError, struct.error) as e:
                raise DBError('invalid float32') from e
            result.append(f)
        return result

    def _keyfile(self, keys):
        path = self.root / ('keys-' + uuid.uuid4().hex + '.bin')
        previous = None
        with path.open('xb') as out:
            for key in keys:
                if previous == key:
                    raise DBError('duplicate primary key')
                out.write(struct.pack('<q', key))
                previous = key
            out.flush()
            os.fsync(out.fileno())
        return path.name

    def _keys(self, table):
        with (self.root / table['keys']).open('rb') as src:
            while True:
                raw = src.read(8)
                if not raw:
                    return
                if len(raw) != 8:
                    raise DBError('incomplete primary index')
                yield struct.unpack('<q', raw)[0]

    def insert(self, name, rows):
        def action():
            from .sql import SQL
            table = self._table(name)
            staged = self._write(self._validate(table, row) for row in rows)
            incoming = dict(table, file=staged, segments=[])
            pk = table['primary']
            # Sort only primary keys, using bounded disk runs for huge statements.
            ordered = SQL(self).external_sort(
                ({pk: row[pk]} for row in self._rows_of(incoming)), lambda row: row[pk])
            newkeys = (row[pk] for row in ordered)
            keys = self._keyfile(heapq.merge(self._keys(table), newkeys))
            segments = table.get('segments', []) + [staged]
            result = dict(table, segments=segments, keys=keys)
            if len(segments) >= 128 or 'index' in table:
                result = dict(result, file=self._write(self._rows_of(result)), segments=[])
            self.pending[name] = self._indexed(result)
            self._collect()
        return self._statement(action)

    def modify(self, name, predicate, updates=None):
        """updates=None deletes matching rows; otherwise updates fixed columns."""
        def action():
            table = self._table(name)
            if updates is not None and (table['primary'] in updates or not set(updates) <= set(table['columns'])):
                raise DBError('invalid update columns')
            if updates is not None:
                # Validate assignments even when the predicate matches no rows.
                probe = {}
                for col, spec in table['columns'].items():
                    probe[col] = 0 if spec[0]=='BIGINT' else '' if spec[0]=='VARCHAR' else [0.0]*spec[1]
                self._validate(table, dict(probe, **updates))
            count = 0
            def changed():
                nonlocal count
                for row in self.rows(name):
                    if predicate(row):
                        count += 1
                        if updates is None:
                            continue
                        row = self._validate(table, dict(row, **updates))
                    yield row
            replacement = self._write(changed())
            result = dict(table, file=replacement, segments=[])
            if updates is None:
                from .sql import SQL
                pk = table['primary']
                ordered = SQL(self).external_sort(({pk:r[pk]} for r in self._rows_of(result)), lambda r:r[pk])
                result['keys'] = self._keyfile(r[pk] for r in ordered)
            self.pending[name] = self._indexed(result)
            self._collect()
            return count
        return self._statement(action)

    def _indexed(self, table):
        if 'index' not in table:
            return table
        if table.get('segments'):
            table = dict(table, file=self._write(self._rows_of(table)), segments=[])
        index = dict(table['index'])
        column = index['column']
        dimension = table['columns'][column][1]
        path = self.root / ('index-' + uuid.uuid4().hex + '.bin')
        record = struct.Struct('<qQ' + 'f' * dimension)
        metadata = path.with_suffix('.meta')
        with (self.root / table['file']).open('rb') as src, path.open('xb') as out, metadata.open('x', encoding='utf-8') as meta:
            while True:
                offset = src.tell()
                line = src.readline()
                if not line:
                    break
                row = json.loads(line)
                out.write(record.pack(row[table['primary']], offset, *row[column]))
                meta.write(encode({key:value for key,value in row.items() if key!=column})+'\n')
            out.flush()
            os.fsync(out.fileno())
            meta.flush()
            os.fsync(meta.fileno())
        index['file'] = path.name
        index['metadata'] = metadata.name
        return dict(table, index=index)

    def create_vector_index(self, name, table_name, column):
        def action():
            table = self._table(table_name)
            spec = table['columns'].get(column)
            if not spec or spec[0] != 'VECTOR' or 'index' in table:
                raise DBError('invalid or existing vector index')
            if any(t.get('index', {}).get('name') == name for t in self.catalog.values()):
                raise DBError('duplicate index name')
            self.pending[table_name] = self._indexed(dict(table, index={'name':name, 'column':column}))
        return self._statement(action)

    def topk(self, name, column, query, k, predicate=lambda row: True):
        table = self._table(name)
        spec = table['columns'].get(column)
        if not spec or spec[0] != 'VECTOR' or type(k) is not int or k < 0:
            raise DBError('invalid TopK')
        query = self.vector(query, spec[1])
        index = table.get('index')
        if index and index['column'] == column:
            return self._index_topk(table, index, query, k, predicate)
        def candidates():
            for row in self.rows(name):
                if predicate(row):
                    squared = sum((a-b)**2 for a,b in zip(row[column], query))
                    yield squared, row[table['primary']], row
        return [(row,math.sqrt(d)) for d,_,row in heapq.nsmallest(k,candidates(),key=lambda x:(x[0],x[1]))]

    def _index_topk(self, table, index, query, k, predicate):
        if k == 0:
            return []
        try:
            import numpy as np
        except ImportError:
            np = None
        dimension = len(query)
        record = struct.Struct('<qQ' + 'f'*dimension)
        def candidates():
            with (self.root/index['file']).open('rb') as src, (self.root/index['metadata']).open(encoding='utf-8') as meta:
                while True:
                    raw = src.read(record.size*1024)
                    if not raw:
                        break
                    if len(raw)%record.size:
                        raise DBError('incomplete vector index')
                    count = len(raw)//record.size
                    mask = []
                    for position in range(count):
                        line = meta.readline()
                        if not line:
                            raise DBError('incomplete index metadata')
                        if predicate(json.loads(line)):
                            mask.append(position)
                    if np is not None:
                        dtype=np.dtype([('pk','<i8'),('offset','<u8'),('vector','<f4',(dimension,))])
                        entries=np.frombuffer(raw,dtype=dtype)
                        selected=entries[mask]
                        delta=selected['vector'].astype(np.float64)-np.asarray(query,dtype=np.float64)
                        squared=np.einsum('ij,ij->i',delta,delta)
                        order=np.lexsort((selected['pk'],squared))[:k]
                        for i in order:
                            yield float(squared[i]),int(selected['pk'][i]),int(selected['offset'][i])
                    else:
                        for position in mask:
                            values=record.unpack_from(raw,position*record.size)
                            yield sum((a-b)**2 for a,b in zip(values[2:],query)),values[0],values[1]
        winners=heapq.nsmallest(k,candidates(),key=lambda x:(x[0],x[1]))
        result=[]
        with (self.root/table['file']).open('rb') as data:
            for distance,pk,offset in winners:
                data.seek(offset)
                result.append((json.loads(data.readline()),math.sqrt(distance)))
        return result
