"""Bounded, physical redo log for immutable files and database-wide catalogs."""
import json
import os
import struct
import zlib


class RedoLog:
    HEADER=struct.Struct('<4sBII')
    FILE=1
    COMMIT=2
    BLOCK=256*1024
    CHECKPOINT_BYTES=8*1024*1024

    def __init__(self, root):
        self.root=root
        self.path=root/'database.wal'
        if not self.path.exists():
            with self.path.open('xb') as out:out.flush();os.fsync(out.fileno())
            fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)

    @classmethod
    def _frame(cls,out,kind,payload):
        checksum=zlib.crc32(payload,zlib.crc32(bytes([kind])))
        out.write(cls.HEADER.pack(b'MVW1',kind,len(payload),checksum))
        out.write(payload)

    def append(self, files, catalog):
        with self.path.open('ab') as out:
            for filename in sorted(files):
                name=filename.encode('utf-8');prefix=struct.pack('<H',len(name))+name
                with (self.root/filename).open('rb') as source:
                    offset=0
                    while True:
                        block=source.read(self.BLOCK)
                        if block or offset==0:self._frame(out,self.FILE,prefix+struct.pack('<Q',offset)+block)
                        offset+=len(block)
                        if len(block)<self.BLOCK:break
            payload=json.dumps(catalog,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
            self._frame(out,self.COMMIT,payload)
            out.flush();os.fdatasync(out.fileno())

    def recover(self, catalog):
        """Replay before cleanup/readiness. A repeated replay is idempotent."""
        committed_end=0
        with self.path.open('r+b') as source:
            while True:
                header=source.read(self.HEADER.size)
                if not header:break
                if len(header)!=self.HEADER.size:break
                magic,kind,size,checksum=self.HEADER.unpack(header)
                if magic!=b'MVW1' or kind not in (self.FILE,self.COMMIT) or size>64*1024*1024:break
                payload=source.read(size)
                if len(payload)!=size or zlib.crc32(payload,zlib.crc32(bytes([kind])))!=checksum:break
                if kind==self.COMMIT:
                    catalog=json.loads(payload)
                    committed_end=source.tell()
                else:
                    length=struct.unpack_from('<H',payload)[0]
                    name=payload[2:2+length].decode('utf-8')
                    if not name or '/' in name or '\\' in name or name in ('.','..'):
                        raise ValueError('invalid WAL filename')
                    offset=struct.unpack_from('<Q',payload,2+length)[0]
                    path=self.root/name
                    with path.open('wb' if offset==0 else 'r+b') as out:
                        out.seek(offset);out.write(payload[10+length:])
            if source.tell()!=committed_end or os.fstat(source.fileno()).st_size!=committed_end:
                source.truncate(committed_end);source.flush();os.fdatasync(source.fileno())
        return catalog

    def reset(self):
        with self.path.open('r+b') as out:
            out.truncate(0);out.flush();os.fdatasync(out.fileno())

    @property
    def size(self):return self.path.stat().st_size
