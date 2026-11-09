"""Disk-backed vector engine with immutable segments, global redo and checkpoints.
All database and temporary files live under data_dir.
"""
import heapq
from collections import OrderedDict
import json
import math
import os
import struct
import uuid
from pathlib import Path

try:
    if os.environ.get('MINIVECTOR_NATIVE')=='0':raise ImportError()
    from .libminivector import float32_vector as _native_vector
except ImportError:
    _native_vector=None
    if os.environ.get('MINIVECTOR_REQUIRE_NATIVE')=='1' and os.environ.get('MINIVECTOR_NATIVE')!='0':
        raise RuntimeError('required native float32 codec could not be loaded')


class DBError(Exception):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


class Database:
    def __init__(self, data_dir, *, wal=True):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest = self.root / 'manifest.json'
        self.committed = json.loads(self.manifest.read_text()) if self.manifest.exists() else {}
        self.pending = None
        self.failed = False
        self._dirty_files=set()
        from .wal import RedoLog
        self.wal=RedoLog(self.root) if wal or (self.root/'database.wal').exists() else None
        if self.wal is not None and self.wal.size:
            self.committed=self.wal.recover(self.committed)
            self._dirty_files.update(self._catalog_files(self.committed))
            self._checkpoint()
        self._filter_cache=OrderedDict()
        self._filter_cache_bytes=0
        self._scan_maps=OrderedDict()
        self._scan_map_bytes=0
        self._query_masks=OrderedDict()
        self._query_mask_bytes=0
        self._collect()
        import shutil
        for abandoned in self.root.glob('sort-*'):
            if abandoned.is_dir():
                shutil.rmtree(abandoned)

    def _catalog_files(self,catalog):
        files=set()
        for table in catalog.values():
            files.update([table['file'],table['keys']])
            files.update(table.get('segments',[]))
            files.update(self._key_name(run) for run in table.get('key_segments',[]))
            for change in table.get('changes',[]):
                files.add(self._key_name(change['keys']))
                if change.get('file'):files.add(change['file'])
            index=table.get('index',{})
            files.update(index[key] for key in ('file','metadata','quantized') if index.get(key))
        return files

    def _sync_file(self,out):
        out.flush()
        if self.wal is None:os.fsync(out.fileno())

    def _collect(self):
        live=self._catalog_files(self.committed)
        if self.pending is not None:live.update(self._catalog_files(self.pending))
        for p in list(self.root.glob('table-*.jsonl')) + list(self.root.glob('table-*.rows')) + list(self.root.glob('index-*.bin')) + list(self.root.glob('keys-*.bin')) + list(self.root.glob('index-*.meta')) + list(self.root.glob('index-*.q8')):
            if p.name not in live:
                p.unlink()
        for key in list(self._filter_cache):
            if key[0] not in live:
                self._filter_cache_bytes-=self._filter_cache.pop(key)[1]
        for key in list(self._scan_maps):
            if key not in live:self._scan_map_bytes-=self._scan_maps.pop(key)[1]
        for key in list(self._query_masks):
            if key[0] not in live:self._query_mask_bytes-=self._query_masks.pop(key)[1]
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
        if self.failed:raise DBError('I/O failure: reopen database before continuing')
        if self.pending is None:
            raise DBError('no transaction')
        self.pending = None
        self._collect()

    def _publish(self,catalog):
        temp=self.root/('manifest-'+uuid.uuid4().hex+'.tmp')
        with temp.open('x',encoding='utf-8') as out:
            out.write(encode(catalog));out.flush();os.fsync(out.fileno())
        os.replace(temp,self.manifest)
        fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _checkpoint(self):
        live=self._catalog_files(self.committed)
        for filename in sorted(live & self._dirty_files):
            with (self.root/filename).open('rb') as source:os.fsync(source.fileno())
        self._publish(self.committed)
        self.wal.reset()
        self._dirty_files.clear()

    def commit(self):
        if self.failed:raise DBError('I/O failure: reopen database before continuing')
        if self.pending is None:raise DBError('no transaction')
        try:
            if self.wal is None:self._publish(self.pending)
            else:
                files=self._catalog_files(self.pending)-self._catalog_files(self.committed)
                self.wal.append(files,self.pending)
                self._dirty_files.update(files)
        except OSError:
            self.failed=True
            raise
        self.committed=self.pending;self.pending=None
        self._collect()
        if self.wal is not None and self.wal.size>=self.wal.CHECKPOINT_BYTES:
            try:self._checkpoint()
            except OSError:self.failed=True;raise

    def close(self):
        if not self.failed and self.pending is not None:
            self.rollback()
        if not self.failed and self.wal is not None and self.wal.size:
            try:self._checkpoint()
            except OSError:self.failed=True;raise
        self._scan_maps.clear();self._scan_map_bytes=0
        self._query_masks.clear();self._query_mask_bytes=0
        self._filter_cache.clear();self._filter_cache_bytes=0

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
    ROW_WITH_KEYS_MAGIC = b'MVDBROW2\n'
    ROW_REFERENCE_MAGIC = b'MVDBREF1\n'
    VECTOR_REFERENCE=1<<31
    ROW_HEADER = struct.Struct('<III')

    @classmethod
    def _read_record(cls, source, scalar_only=False):
        header = source.read(cls.ROW_HEADER.size)
        if not header:
            return None
        if len(header) != cls.ROW_HEADER.size:
            raise DBError('incomplete row header')
        scalar_size, name_size, dimension = cls.ROW_HEADER.unpack(header)
        scalar = source.read(scalar_size)
        if len(scalar)!=scalar_size:raise DBError('incomplete row data')
        if scalar_only:
            source.seek(name_size,os.SEEK_CUR)
            if dimension & cls.VECTOR_REFERENCE:
                length=source.read(2)
                if len(length)!=2:raise DBError('incomplete vector reference')
                source.seek(struct.unpack('<H',length)[0]+8,os.SEEK_CUR)
            else:source.seek(dimension*4,os.SEEK_CUR)
            return json.loads(scalar)
        name = source.read(name_size)
        vector = cls._vector_bytes(source,dimension)
        dimension &= ~cls.VECTOR_REFERENCE
        if len(scalar)!=scalar_size or len(name)!=name_size or len(vector)!=dimension*4:
            raise DBError('incomplete row data')
        row = json.loads(scalar)
        if dimension:
            row[name.decode('utf-8')] = list(struct.unpack('<'+str(dimension)+'f',vector))
        return row

    @classmethod
    def _vector_bytes(cls,source,dimension):
        if not dimension & cls.VECTOR_REFERENCE:return source.read(dimension*4)
        length=source.read(2)
        if len(length)!=2:raise DBError('incomplete vector reference')
        size=struct.unpack('<H',length)[0];raw=source.read(size+8)
        if len(raw)!=size+8:raise DBError('incomplete vector reference')
        name=raw[:size].decode('utf-8')
        if not name or '/' in name or '\\' in name:raise DBError('invalid vector reference')
        offset=struct.unpack_from('<Q',raw,size)[0]
        with (Path(source.name).parent/name).open('rb') as vector:
            vector.seek(offset);return vector.read((dimension & ~cls.VECTOR_REFERENCE)*4)

    @classmethod
    def _row_span(cls, source):
        magic=source.read(len(cls.ROW_MAGIC))
        if magic in (cls.ROW_MAGIC,cls.ROW_REFERENCE_MAGIC):return None
        if magic==cls.ROW_WITH_KEYS_MAGIC:
            raw=source.read(8)
            if len(raw)!=8:raise DBError('incomplete row span')
            end=struct.unpack('<Q',raw)[0]
            if end<source.tell() or end>os.fstat(source.fileno()).st_size:
                raise DBError('invalid row span')
            return end
        raise DBError('unknown row format')

    def _write(self, rows, key_column=None):
        path = self._newfile()
        count=0;prefix=[];large=False
        with path.open('x+b') as out:
            out.write(self.ROW_MAGIC if key_column is None else self.ROW_WITH_KEYS_MAGIC+b'\0'*8)
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
                count+=1
                if key_column is not None and not large:
                    if count<=4096:prefix.append(row[key_column])
                    else:large=True;prefix.clear()
            data_end=out.tell()
            if key_column is not None:
                out.seek(len(self.ROW_WITH_KEYS_MAGIC))
                out.write(struct.pack('<Q',data_end))
                out.seek(data_end)
                out.flush()  # Publish only to this process's private staging reader, not the catalog.
                if large:
                    from .sql import SQL
                    key_rows=({key_column:row[key_column]} for row in self._rows_of({'file':path.name},scalar_only=True))
                    ordered=SQL(self).external_sort(key_rows,lambda row:row[key_column])
                    keys=(row[key_column] for row in ordered)
                else:keys=iter(sorted(prefix))
                previous=None
                for key in keys:
                    if key==previous:raise DBError('duplicate primary key')
                    out.write(struct.pack('<q',key));previous=key
            self._sync_file(out)
        if key_column is None:return path.name
        return path.name,{'file':path.name,'key_offset':data_end,'key_count':count}

    def _merge_rows(self, table):
        """Copy immutable binary record spans; omit embedded primary-key footers."""
        filenames=[table['file']]+table.get('segments',[])
        if table.get('changes') or any(name.endswith('.jsonl') for name in filenames):
            return self._write(self._rows_of(table))
        path=self._newfile()
        with path.open('xb') as out:
            out.write(self.ROW_MAGIC)
            for name in filenames:
                with (self.root/name).open('rb') as src:
                    end=self._row_span(src)
                    if end is None:end=os.fstat(src.fileno()).st_size
                    remaining=end-src.tell()
                    while remaining:
                        block=src.read(min(256*1024,remaining))
                        if not block:raise DBError('incomplete row data')
                        out.write(block);remaining-=len(block)
            self._sync_file(out)
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

    def rows(self, name, *, scalar_only=False):
        table = self._table(name)
        yield from self._rows_of(table,scalar_only=scalar_only)

    def _base_rows(self, table, scalar_only=False):
        for filename in [table['file']] + table.get('segments', []):
            if filename.endswith('.jsonl'):
                # Existing snapshots remain readable after the format upgrade.
                with (self.root / filename).open(encoding='utf-8') as src:
                    for line in src:
                        yield json.loads(line)
            else:
                with (self.root / filename).open('rb') as src:
                    end=self._row_span(src)
                    while end is None or src.tell()<end:
                        row=self._read_record(src,scalar_only=scalar_only)
                        if row is None:break
                        if end is not None and src.tell()>end:raise DBError('row crosses key footer')
                        yield row

    def _rows_of(self,table,scalar_only=False,*,changes_only=False):
        changes=table.get('changes',[])
        if not changes:
            if not changes_only:yield from self._base_rows(table,scalar_only=scalar_only)
            return
        import contextlib
        with contextlib.ExitStack() as stack:
            runs=[]
            for change in changes:
                run=change['keys'];source=stack.enter_context((self.root/self._key_name(run)).open('rb'))
                start=run['key_offset'] if isinstance(run,dict) else 0
                count=run['key_count'] if isinstance(run,dict) else os.fstat(source.fileno()).st_size//8
                runs.append((source,start,count))
            sources=[] if changes_only else [(table,runs)]
            sources.extend(({'file':change['file']},runs[i+1:]) for i,change in enumerate(changes) if change.get('file'))
            for source,later in sources:
                for row in self._base_rows(source,scalar_only=scalar_only):
                    if not any(self._contains_key(run,row[table['primary']]) for run in later):yield row

    @staticmethod
    def _contains_key(run,key):
        source,start,count=run;lo=0;hi=count
        while lo<hi:
            middle=(lo+hi)//2;source.seek(start+middle*8);raw=source.read(8)
            if len(raw)!=8:raise DBError('incomplete primary index')
            value=struct.unpack('<q',raw)[0]
            if value<key:lo=middle+1
            else:hi=middle
        if lo>=count:return False
        source.seek(start+lo*8)
        return struct.unpack('<q',source.read(8))[0]==key

    def _consolidate(self,table):
        file=self._merge_rows(table)
        result=dict(table,file=file,segments=[],segment_levels={},changes=[])
        if table.get('changes'):
            from .sql import SQL
            pk=table['primary']
            rows=({pk:row[pk]} for row in self._base_rows(result,scalar_only=True))
            ordered=SQL(self).external_sort(rows,lambda row:row[pk])
            result=dict(result,keys=self._keyfile(row[pk] for row in ordered),key_segments=[])
        return result

    CHANGE_LIMIT=8

    def _modify_delta(self,table,predicate,updates):
        from .sql import SQL
        pk=table['primary'];count=0;total=0
        def matched():
            nonlocal count,total
            scalar_only=getattr(predicate,'terms',None) is not None and (updates is None or
                all(spec[0]!='VECTOR' or column in updates for column,spec in table['columns'].items()))
            for row in self._rows_of(table,scalar_only=scalar_only):
                total+=1
                if predicate(row):
                    count+=1
                    yield {pk:row[pk]} if updates is None else self._validate(table,dict(row,**updates))
        if updates is None:
            ordered=SQL(self).external_sort(matched(),lambda row:row[pk])
            change={'keys':self._keyfile(row[pk] for row in ordered)}
        else:
            file,keys=self._write(matched(),key_column=pk)
            change={'file':file,'keys':keys}
        if not count:return table,0
        result=dict(table,changes=table.get('changes',[])+[change])
        if len(result['changes'])>=self.CHANGE_LIMIT or count>max(64,total//4):
            result=self._consolidate(result)
        return result,count

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
        if isinstance(value,str):
            try:value=json.loads(value)
            except ValueError as error:raise DBError('invalid vector literal') from error
        if not isinstance(value, (list, tuple)) or len(value) != dimension:
            raise DBError('vector dimension mismatch')
        try:
            if _native_vector is not None and type(value) in (list,tuple):
                return _native_vector(value,dimension)
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
            self._sync_file(out)
        return path.name

    @staticmethod
    def _key_name(run):
        return run['file'] if isinstance(run,dict) else run

    def _filekeys(self, run):
        with (self.root / self._key_name(run)).open('rb') as src:
            remaining=None
            if isinstance(run,dict):
                src.seek(run['key_offset']);remaining=run['key_count']
            while remaining is None or remaining>0:
                raw=src.read(8192 if remaining is None else min(8192,remaining*8))
                if not raw:
                    if remaining:raise DBError('incomplete primary index')
                    return
                if len(raw)%8:raise DBError('incomplete primary index')
                for (key,) in struct.iter_unpack('<q',raw):yield key
                if remaining is not None:remaining-=len(raw)//8

    def _keys(self, table):
        yield from heapq.merge(*(self._filekeys(f) for f in [table['keys']]+table.get('key_segments',[])))

    def _key_range(self, run):
        if isinstance(run,dict) and not run['key_count']:return None
        with (self.root/self._key_name(run)).open('rb') as src:
            if isinstance(run,dict):src.seek(run['key_offset'])
            first=src.read(8)
            if not first:return None
            if isinstance(run,dict):src.seek(run['key_offset']+8*(run['key_count']-1))
            else:src.seek(-8,os.SEEK_END)
            return struct.unpack('<q',first)[0],struct.unpack('<q',src.read(8))[0]

    def _detach_keys(self, table):
        if any(isinstance(run,dict) for run in table.get('key_segments',[])):
            return dict(table,keys=self._keyfile(self._keys(table)),key_segments=[])
        return table

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

    SEGMENT_FANOUT=128
    SEGMENT_LIMIT=128

    def _compact_segments(self,table):
        segments=list(table.get('segments',[]))
        levels={name:table.get('segment_levels',{}).get(name,0) for name in segments}
        while True:
            counts={}
            for name in segments:counts[levels[name]]=counts.get(levels[name],0)+1
            ready=[level for level,count in counts.items() if count>=self.SEGMENT_FANOUT]
            if not ready and len(segments)>=self.SEGMENT_LIMIT:
                ready=[level for level,count in counts.items() if count>=2]
            if not ready:break
            level=min(ready)
            chosen=[name for name in segments if levels[name]==level][:self.SEGMENT_FANOUT]
            merged=self._merge_rows({'file':chosen[0],'segments':chosen[1:]})
            position=segments.index(chosen[0]);selected=set(chosen)
            # Preserve the relative order of untouched immutable segments.
            segments=[name for name in segments if name not in selected]
            segments.insert(position,merged)
            for name in chosen:del levels[name]
            levels[merged]=level+1
        if len(segments)>self.SEGMENT_LIMIT:
            file=self._merge_rows(dict(table,segments=segments))
            return self._detach_keys(dict(table,file=file,segments=[],segment_levels={}))
        return dict(table,segments=segments,segment_levels=levels)

    def insert(self, name, rows):
        def action():
            from .sql import SQL
            table = self._table(name)
            if table.get('changes'):table=self._consolidate(table)
            pk = table['primary']
            staged,incoming_keys=self._write((self._validate(table,row) for row in rows),key_column=pk)
            self._check_keys(table,incoming_keys)
            key_segments=table.get('key_segments',[])+[incoming_keys]
            keys=table['keys']
            if len(key_segments)>=32:
                keys=self._keyfile(heapq.merge(self._keys(table),self._filekeys(incoming_keys)))
                key_segments=[]
            segments = table.get('segments', []) + [staged]
            result = dict(table, segments=segments, keys=keys, key_segments=key_segments)
            if 'index' in table:
                result = self._detach_keys(dict(result, file=self._merge_rows(result), segments=[],segment_levels={}))
            else:
                result=self._compact_segments(result)
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
            result,count=self._modify_delta(table,predicate,updates)
            self.pending[name]=self._indexed(result) if count and not result.get('changes') else result
            self._collect()
            return count
        return self._statement(action)

    def _raw_records(self,table,column):
        for filename in [table['file']]+table.get('segments',[]):
            if filename.endswith('.jsonl'):
                for row in self._base_rows({'file':filename}):
                    scalar=encode({k:v for k,v in row.items() if k!=column}).encode('utf-8')
                    yield scalar,column.encode('utf-8'),struct.pack('<'+'f'*len(row[column]),*row[column])
                continue
            with (self.root/filename).open('rb') as source:
                end=self._row_span(source)
                while end is None or source.tell()<end:
                    header=source.read(self.ROW_HEADER.size)
                    if not header:break
                    if len(header)!=self.ROW_HEADER.size:raise DBError('incomplete row header')
                    scalar_size,name_size,dimension=self.ROW_HEADER.unpack(header)
                    scalar=source.read(scalar_size);name=source.read(name_size)
                    vector=self._vector_bytes(source,dimension)
                    if len(scalar)!=scalar_size or len(name)!=name_size or len(vector)!=(dimension & ~self.VECTOR_REFERENCE)*4:
                        raise DBError('incomplete row data')
                    if end is not None and source.tell()>end:raise DBError('row crosses key footer')
                    yield scalar,name,vector

    def _owned_index(self,table):
        if table.get('changes'):table=self._consolidate(table)
        table=self._detach_keys(table)
        index=dict(table['index']);column=index['column'];dimension=table['columns'][column][1]
        path=self.root/('index-'+uuid.uuid4().hex+'.bin');metadata=path.with_suffix('.meta')
        rows_path=self._newfile();name=path.name.encode('utf-8');column_bytes=column.encode('utf-8')
        prefix=struct.Struct('<qQ');reference=struct.pack('<H',len(name))+name
        with path.open('xb') as out,metadata.open('xb') as meta,rows_path.open('xb') as rows:
            rows.write(self.ROW_REFERENCE_MAGIC)
            for scalar,column_name,vector in self._raw_records(table,column):
                if column_name!=column_bytes or len(vector)!=dimension*4:raise DBError('invalid vector record')
                key=json.loads(scalar)[table['primary']];offset=rows.tell();vector_offset=out.tell()+16
                out.write(prefix.pack(key,offset));out.write(vector)
                rows.write(self.ROW_HEADER.pack(len(scalar),len(column_bytes),dimension|self.VECTOR_REFERENCE))
                rows.write(scalar);rows.write(column_bytes);rows.write(reference);rows.write(struct.pack('<Q',vector_offset))
                meta.write(scalar+b'\n')
            for stream in (out,meta,rows):self._sync_file(stream)
        index.update(file=path.name,metadata=metadata.name,layout='owned');index.pop('quantized',None)
        from .native import build_quantized
        quantized=path.with_suffix('.q8')
        if build_quantized(path,quantized,dimension,self.SCAN_MAP_LIMIT,sync=self.wal is None):index['quantized']=quantized.name
        return dict(table,file=rows_path.name,segments=[],segment_levels={},changes=[],index=index)

    INDEX_LAYOUT='owned'

    def _indexed(self, table):
        if 'index' not in table:
            return table
        if self.INDEX_LAYOUT=='owned':return self._owned_index(table)
        if table.get('segments') or table.get('changes'):
            table=self._consolidate(table)
        table=self._detach_keys(table)
        index = dict(table['index'])
        column = index['column']
        dimension = table['columns'][column][1]
        path = self.root / ('index-' + uuid.uuid4().hex + '.bin')
        record = struct.Struct('<qQ' + 'f' * dimension)
        prefix = struct.Struct('<qQ')
        column_bytes=column.encode('utf-8')
        metadata = path.with_suffix('.meta')
        with (self.root / table['file']).open('rb') as src, path.open('xb') as out, metadata.open('xb') as meta:
            binary=table['file'].endswith('.rows')
            end=self._row_span(src) if binary else None
            while end is None or src.tell()<end:
                offset = src.tell()
                if binary:
                    header=src.read(self.ROW_HEADER.size)
                    if not header:break
                    if len(header)!=self.ROW_HEADER.size:raise DBError('incomplete row header')
                    scalar_size,name_size,stored_dimension=self.ROW_HEADER.unpack(header)
                    if stored_dimension!=dimension:raise DBError('vector dimension mismatch')
                    scalar=src.read(scalar_size);name=src.read(name_size);vector=src.read(dimension*4)
                    if len(scalar)!=scalar_size or len(name)!=name_size or len(vector)!=dimension*4:
                        raise DBError('incomplete row data')
                    if end is not None and src.tell()>end:raise DBError('row crosses key footer')
                    if name!=column_bytes:raise DBError('invalid vector column')
                    key=json.loads(scalar)[table['primary']]
                    out.write(prefix.pack(key,offset));out.write(vector)
                    meta.write(scalar+b'\n')
                else:
                    line=src.readline()
                    if not line:break
                    row=json.loads(line)
                    out.write(record.pack(row[table['primary']], offset, *row[column]))
                    meta.write((encode({key:value for key,value in row.items() if key!=column})+'\n').encode('utf-8'))
            self._sync_file(out)
            self._sync_file(meta)
        index.pop('layout',None)
        index['file'] = path.name
        index['metadata'] = metadata.name
        index.pop('quantized',None)
        from .native import build_quantized
        quantized=path.with_suffix('.q8')
        if build_quantized(path,quantized,dimension,self.SCAN_MAP_LIMIT,sync=self.wal is None):
            index['quantized']=quantized.name
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

    def topk(self, name, column, query, k, predicate=lambda row: True, *, scalar_only=False, primary_only=False):
        table = self._table(name)
        spec = table['columns'].get(column)
        if not spec or spec[0] != 'VECTOR' or type(k) is not int or k < 0:
            raise DBError('invalid TopK')
        query = self.vector(query, spec[1])
        index = table.get('index')
        if index and index['column'] == column:
            if table.get('changes'):
                return self._delta_topk(table,index,query,k,predicate,scalar_only,primary_only)
            return self._index_topk(table, index, query, k, predicate,scalar_only=scalar_only,primary_only=primary_only)
        def candidates():
            for row in self.rows(name):
                if predicate(row):
                    squared = sum((a-b)**2 for a,b in zip(row[column], query))
                    yield squared, row[table['primary']], row
        return [(row,math.sqrt(d)) for d,_,row in heapq.nsmallest(k,candidates(),key=lambda x:(x[0],x[1]))]

    def _delta_topk(self,table,index,query,k,predicate,scalar_only,primary_only):
        import contextlib
        if not k:return []
        with contextlib.ExitStack() as stack:
            runs=[]
            for change in table['changes']:
                run=change['keys'];source=stack.enter_context((self.root/self._key_name(run)).open('rb'))
                start=run['key_offset'] if isinstance(run,dict) else 0
                count=run['key_count'] if isinstance(run,dict) else os.fstat(source.fileno()).st_size//8
                runs.append((source,start,count))
            pk=table['primary']
            def visible(row):
                return predicate(row) and not any(self._contains_key(run,row[pk]) for run in runs)
            base=self._index_topk(table,index,query,k,visible,scalar_only=scalar_only,primary_only=primary_only,squared=True)
            # Use the base index's accumulation order for identical-vector ties.
            def distance_of(vector):return sum((a-b)**2 for a,b in zip(vector,query))
            try:
                import numpy as np
                from .native import available,scan
                if available():
                    entry=np.zeros(1,dtype=[('pk','<i8'),('offset','<u8'),('vector','<f4',(len(query),))])
                    query_array=np.asarray(query,dtype=np.float64)
                    def distance_of(vector):
                        entry['vector'][0]=vector
                        return next(scan(entry,query_array,slice(None),1))[0]
                else:
                    def distance_of(vector):
                        delta=np.asarray(vector,dtype=np.float64)-np.asarray(query,dtype=np.float64)
                        return float(np.einsum('i,i->',delta,delta))
            except ImportError:pass
            def candidates():
                for row,distance in base:yield distance,row[pk],row
                for row in self._rows_of(table,changes_only=True):
                    if predicate(row):
                        distance=distance_of(row[index['column']])
                        yield distance,row[pk],row
            winners=heapq.nsmallest(k,candidates(),key=lambda hit:(hit[0],hit[1]))
            return [({pk:key} if primary_only else row,math.sqrt(distance)) for distance,key,row in winners]

    FILTER_CACHE_LIMIT=4*1024*1024

    def _filter_chunk(self, meta, count, terms, np, predicate):
        import sys
        columns=tuple(sorted({term[0] for term in terms}))
        key=(Path(meta.name).name,meta.tell(),columns)
        cached=self._filter_cache.get(key)
        if cached is not None:
            arrays,size,end=cached
            self._filter_cache.move_to_end(key)
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
            size=512+sys.getsizeof(columns)+sum(sys.getsizeof(column) for column in columns)
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

    SCAN_MAP_LIMIT=8*1024*1024
    QUERY_MASK_LIMIT=2*1024*1024
    WHOLE_MASK_ROWS=262144

    def _scan_map(self,index,dtype,np):
        name=index['file']
        cached=self._scan_maps.get(name)
        if cached is not None:
            self._scan_maps.move_to_end(name)
            return cached[0]
        path=self.root/name
        size=path.stat().st_size
        if size%dtype.itemsize:raise DBError('incomplete vector index')
        if not size:return None
        mapped=np.memmap(path,dtype=dtype,mode='r')
        if size<=self.SCAN_MAP_LIMIT:
            while self._scan_maps and self._scan_map_bytes+size>self.SCAN_MAP_LIMIT:
                self._scan_map_bytes-=self._scan_maps.popitem(last=False)[1][1]
            self._scan_maps[name]=(mapped,size);self._scan_map_bytes+=size
        return mapped

    def _query_mask(self,index,count,terms,np,predicate):
        import sys
        key=(index['file'],tuple(terms))
        cached=self._query_masks.get(key)
        if cached is not None:
            self._query_masks.move_to_end(key)
            return cached[0]
        mask=np.empty(count,dtype=np.uint8)
        with (self.root/index['metadata']).open(encoding='utf-8') as meta:
            for start in range(0,count,2048):
                size=min(2048,count-start)
                mask[start:start+size]=self._filter_chunk(meta,size,terms,np,predicate)
        # Store selective filters as postings; native queries visit matching rows
        # directly instead of checking every entry in the full-table bitmap.
        if count and np.count_nonzero(mask)*8<=count:
            mask=np.flatnonzero(mask).astype(np.uint32)
        mask.flags.writeable=False
        size=mask.nbytes+sys.getsizeof(mask)+512+sys.getsizeof(key[1])+sum(sys.getsizeof(term) for term in terms)+sum(sys.getsizeof(value) for term in terms for value in term)
        if size<=self.QUERY_MASK_LIMIT:
            while self._query_masks and self._query_mask_bytes+size>self.QUERY_MASK_LIMIT:
                self._query_mask_bytes-=self._query_masks.popitem(last=False)[1][1]
            self._query_masks[key]=(mask,size);self._query_mask_bytes+=size
        return mask

    def _index_topk(self, table, index, query, k, predicate, *, scalar_only=False, primary_only=False,squared=False):
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
        winners=None
        if np is not None and terms is not None:
            from .native import available,scan
            if available():
                mapped=self._scan_map(index,dtype,np)
                if mapped is None:return []
                if not terms or len(mapped)<=self.WHOLE_MASK_ROWS:
                    mask=self._query_mask(index,len(mapped),terms,np,predicate) if terms else slice(None)
                    quantization=None
                    candidate_count=len(mask) if isinstance(mask,np.ndarray) and mask.dtype==np.dtype('uint32') else len(mapped)
                    if index.get('quantized') and candidate_count>=512 and k<=candidate_count//4:
                        from .native import QUANT_HEADER,QUANT_MAGIC
                        codes=self._scan_map({'file':index['quantized']},np.dtype('uint8'),np)
                        if codes is None or len(codes)<QUANT_HEADER.size:raise DBError('incomplete quantized index')
                        magic,count,dim,minimum,step=QUANT_HEADER.unpack_from(codes)
                        if magic!=QUANT_MAGIC or count!=len(mapped) or dim!=dimension or len(codes)!=QUANT_HEADER.size+count*dim:
                            raise DBError('invalid quantized index')
                        quantization=(codes[QUANT_HEADER.size:],minimum,step)
                    winners=list(scan(mapped,query_array,mask,k,quantization=quantization))
        def candidates():
            with (self.root/index['file']).open('rb') as src, (self.root/index['metadata']).open(encoding='utf-8') as meta:
                file_size=os.fstat(src.fileno()).st_size
                if file_size%record.size:raise DBError('incomplete vector index')
                if not file_size:return
                mapped=self._scan_map(index,dtype,np) if np is not None else None
                block_rows=2048
                for start in range(0,file_size//record.size,block_rows):
                    count=min(block_rows,file_size//record.size-start)
                    if mapped is None:
                        raw=src.read(record.size*count)
                    else:
                        entries=mapped[start:start+count]
                    if np is not None and terms is not None:
                        mask=self._filter_chunk(meta,count,terms,np,predicate) if terms else slice(None)
                    else:
                        mask=[]
                        for position in range(count):
                            line=meta.readline()
                            if not line:raise DBError('incomplete index metadata')
                            if predicate(json.loads(line)):mask.append(position)
                    if np is not None:
                        from .native import scan
                        native=scan(entries,query_array,mask,k)
                        if native is not None:
                            yield from native
                            continue
                        selected=entries[mask]
                        delta=np.subtract(selected['vector'],query_array,dtype=np.float64)
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
        if winners is None:winners=heapq.nsmallest(k,candidates(),key=lambda x:(x[0],x[1]))
        if primary_only:
            return [({table['primary']:pk},distance if squared else math.sqrt(distance)) for distance,pk,offset in winners]
        result=[]
        with (self.root/table['file']).open('rb') as data:
            for distance,pk,offset in winners:
                data.seek(offset)
                row=self._read_record(data,scalar_only=scalar_only) if table['file'].endswith('.rows') else json.loads(data.readline())
                result.append((row,distance if squared else math.sqrt(distance)))
        return result
