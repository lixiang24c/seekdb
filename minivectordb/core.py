"""Disk-backed reference engine. Immutable generations, atomic manifest commit.

This correctness-first implementation rewrites affected tables; it is not yet the
performance implementation. All database and temporary files live under data_dir.
"""
import heapq
from collections import OrderedDict
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
        self._filter_cache=OrderedDict()
        self._filter_cache_bytes=0
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
        live.update(f for v in self.committed.values() for f in v.get('key_segments',[]))
        live.update(f for v in self.committed.values() for f in v.get('segments', []))
        if self.pending is not None:
            live.update(v['file'] for v in self.pending.values())
            live.update(v['index']['file'] for v in self.pending.values() if 'index' in v)
            live.update(v['index']['metadata'] for v in self.pending.values() if 'index' in v)
            live.update(v['keys'] for v in self.pending.values())
            live.update(f for v in self.pending.values() for f in v.get('key_segments',[]))
            live.update(f for v in self.pending.values() for f in v.get('segments', []))
        for p in list(self.root.glob('table-*.jsonl')) + list(self.root.glob('table-*.rows')) + list(self.root.glob('index-*.bin')) + list(self.root.glob('keys-*.bin')) + list(self.root.glob('index-*.meta')):
            if p.name not in live:
                p.unlink()
        for key in list(self._filter_cache):
            if key[0] not in live:
                self._filter_cache_bytes-=self._filter_cache.pop(key)[1]
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
        return self.root / ('table-' + uuid.uuid4().hex + '.rows')

    ROW_MAGIC = b'MVDBROW1\n'
    ROW_HEADER = struct.Struct('<III')

    @classmethod
    def _read_record(cls, source):
        header = source.read(cls.ROW_HEADER.size)
        if not header:
            return None
        if len(header) != cls.ROW_HEADER.size:
            raise DBError('incomplete row header')
        scalar_size, name_size, dimension = cls.ROW_HEADER.unpack(header)
        scalar = source.read(scalar_size)
        name = source.read(name_size)
        vector = source.read(dimension * 4)
        if len(scalar)!=scalar_size or len(name)!=name_size or len(vector)!=dimension*4:
            raise DBError('incomplete row data')
        row = json.loads(scalar)
        if dimension:
            row[name.decode('utf-8')] = list(struct.unpack('<'+str(dimension)+'f',vector))
        return row

    def _write(self, rows):
        path = self._newfile()
        with path.open('xb') as out:
            out.write(self.ROW_MAGIC)
            for row in rows:
                column = next((key for key,value in row.items() if isinstance(value,list)),None)
                name = column.encode('utf-8') if column is not None else b''
                vector = row[column] if column is not None else []
                scalar = encode({key:value for key,value in row.items() if key!=column}).encode('utf-8')
                out.write(self.ROW_HEADER.pack(len(scalar),len(name),len(vector)))
                out.write(scalar)
                out.write(name)
                if vector:
                    out.write(struct.pack('<'+str(len(vector))+'f',*vector))
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
            if filename.endswith('.jsonl'):
                # Existing snapshots remain readable after the format upgrade.
                with (self.root / filename).open(encoding='utf-8') as src:
                    for line in src:
                        yield json.loads(line)
            else:
                with (self.root / filename).open('rb') as src:
                    if src.read(len(self.ROW_MAGIC))!=self.ROW_MAGIC:
                        raise DBError('unknown row format')
                    while True:
                        row=self._read_record(src)
                        if row is None:break
                        yield row

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
        try:
            if any(type(number) not in (float,int) for number in value):
                raise ValueError()
            fmt='<'+str(dimension)+'f'
            result=list(struct.unpack(fmt,struct.pack(fmt,*value)))
            if not all(math.isfinite(number) for number in result):
                raise ValueError()
        except (OverflowError,ValueError,struct.error) as error:
            raise DBError('invalid float32') from error
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

    def _filekeys(self, filename):
        with (self.root / filename).open('rb') as src:
            while True:
                raw = src.read(8)
                if not raw:return
                if len(raw)!=8:raise DBError('incomplete primary index')
                yield struct.unpack('<q',raw)[0]

    def _keys(self, table):
        yield from heapq.merge(*(self._filekeys(f) for f in [table['keys']]+table.get('key_segments',[])))

    def _key_range(self, filename):
        with (self.root/filename).open('rb') as src:
            first=src.read(8)
            if not first:return None
            src.seek(-8,os.SEEK_END)
            return struct.unpack('<q',first)[0],struct.unpack('<q',src.read(8))[0]

    def _check_keys(self, table, incoming):
        bounds=self._key_range(incoming)
        if bounds is None:return
        for filename in [table['keys']]+table.get('key_segments',[]):
            old=self._key_range(filename)
            if old is None or old[1]<bounds[0] or bounds[1]<old[0]:continue
            previous=None
            for key in heapq.merge(self._filekeys(filename),self._filekeys(incoming)):
                if key==previous:raise DBError('duplicate primary key')
                previous=key

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
            incoming_keys=self._keyfile(newkeys)
            self._check_keys(table,incoming_keys)
            key_segments=table.get('key_segments',[])+[incoming_keys]
            keys=table['keys']
            if len(key_segments)>=32:
                keys=self._keyfile(heapq.merge(self._keys(table),self._filekeys(incoming_keys)))
                key_segments=[]
            segments = table.get('segments', []) + [staged]
            result = dict(table, segments=segments, keys=keys, key_segments=key_segments)
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
                result['key_segments']=[]
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
            binary=table['file'].endswith('.rows')
            if binary and src.read(len(self.ROW_MAGIC))!=self.ROW_MAGIC:
                raise DBError('unknown row format')
            while True:
                offset = src.tell()
                row=self._read_record(src) if binary else json.loads(line) if (line:=src.readline()) else None
                if row is None:
                    break
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

    FILTER_CACHE_LIMIT=4*1024*1024

    def _filter_chunk(self, meta, count, terms, np, predicate):
        import sys
        columns=tuple(sorted({term[0] for term in terms}))
        key=(Path(meta.name).name,meta.tell(),columns)
        cached=self._filter_cache.pop(key,None)
        if cached is not None:
            arrays,size,end=cached
            self._filter_cache[key]=cached
            meta.seek(end)
        else:
            values_by_column={column:[] for column in columns}
            fallback_mask=np.ones(count,dtype=bool)
            temporary_bytes=512
            cacheable=True
            for position in range(count):
                line=meta.readline()
                if not line:raise DBError('incomplete index metadata')
                row=json.loads(line)
                fallback_mask[position]=predicate(row)
                if cacheable:
                    temporary_bytes+=sum(sys.getsizeof(row[column])+8 for column in columns)
                    if temporary_bytes>self.FILTER_CACHE_LIMIT:
                        cacheable=False
                        values_by_column.clear()
                    else:
                        for column in columns:values_by_column[column].append(row[column])
            if not cacheable:
                return fallback_mask
            arrays={}
            size=512
            for column,values in values_by_column.items():
                numeric=not values or type(values[0]) is int
                array=np.asarray(values,dtype=np.int64 if numeric else object)
                arrays[column]=array
                size+=array.nbytes+sys.getsizeof(array)
                if not numeric:size+=sum(sys.getsizeof(value) for value in values)
            end=meta.tell()
            if size<=self.FILTER_CACHE_LIMIT:
                while self._filter_cache and self._filter_cache_bytes+size>self.FILTER_CACHE_LIMIT:
                    self._filter_cache_bytes-=self._filter_cache.popitem(last=False)[1][1]
                self._filter_cache[key]=(arrays,size,end)
                self._filter_cache_bytes+=size
        mask=np.ones(count,dtype=bool)
        for column,op,value in terms:
            array=arrays[column]
            if array.dtype==np.dtype('int64') and (value<-(1<<63) or value>=(1<<63)):
                outside_high=value>=(1<<63)
                passes=(op in ('<','<=') if outside_high else op in ('>','>='))
                if not passes:mask[:]=False
                continue
            if op=='=':mask &= array==value
            elif op=='<':mask &= array<value
            elif op=='<=':mask &= array<=value
            elif op=='>':mask &= array>value
            elif op=='>=':mask &= array>=value
        return mask

    def _index_topk(self, table, index, query, k, predicate):
        if k == 0:
            return []
        try:
            import numpy as np
        except ImportError:
            np = None
        dimension = len(query)
        record = struct.Struct('<qQ' + 'f'*dimension)
        terms=getattr(predicate,'terms',None)
        query_array=np.asarray(query,dtype=np.float64) if np is not None else None
        dtype=np.dtype([('pk','<i8'),('offset','<u8'),('vector','<f4',(dimension,))]) if np is not None else None
        def candidates():
            with (self.root/index['file']).open('rb') as src, (self.root/index['metadata']).open(encoding='utf-8') as meta:
                while True:
                    raw = src.read(record.size*1024)
                    if not raw:
                        break
                    if len(raw)%record.size:
                        raise DBError('incomplete vector index')
                    count = len(raw)//record.size
                    if np is not None and terms is not None:
                        mask=self._filter_chunk(meta,count,terms,np,predicate) if terms else slice(None)
                    else:
                        mask=[]
                        for position in range(count):
                            line=meta.readline()
                            if not line:raise DBError('incomplete index metadata')
                            if predicate(json.loads(line)):mask.append(position)
                    if np is not None:
                        entries=np.frombuffer(raw,dtype=dtype)
                        selected=entries[mask]
                        delta=selected['vector'].astype(np.float64)-query_array
                        squared=np.einsum('ij,ij->i',delta,delta)
                        if len(squared)>k:
                            boundary=np.partition(squared,k-1)[k-1]
                            eligible=np.flatnonzero(squared<=boundary)
                        else:
                            eligible=np.arange(len(squared))
                        order=eligible[np.lexsort((selected['pk'][eligible],squared[eligible]))[:k]]
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
                row=self._read_record(data) if table['file'].endswith('.rows') else json.loads(data.readline())
                result.append((row,math.sqrt(distance)))
        return result
