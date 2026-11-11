#!/usr/bin/env python3
"""Mapped index buffers: bitwise results, lifetimes and buffered fallbacks."""
import json,subprocess
from pathlib import Path
PROBE=r'''
import hashlib,json,math,os,random,struct
from pathlib import Path
from minidb import indexio
from minidb.native import Screen
magic=b'MVFLT001';rng=random.Random(20261210);checks=0
for dim in (16,128,129,4096):
    reader=indexio.IndexReader()
    fmt=struct.Struct('<q'+str(dim)+'f');q=[rng.randrange(-8,9)/4 for _ in range(dim)]
    vectors=[[rng.choice([0.,-0.,2.**-149,-2.**-149,1.,-1.,1e30,-1e30]) for _ in range(dim)] for _ in range(13)]
    vectors=[list(struct.unpack('<'+str(dim)+'f',struct.pack('<'+str(dim)+'f',*v))) for v in vectors]
    payload=b''.join(fmt.pack(i,*v) for i,v in enumerate(vectors));path=Path('/data/test.index');path.write_bytes(magic+payload)
    before=hashlib.sha256(path.read_bytes()).digest();screen=Screen(q);block=fmt.size*max(1,65536//fmt.size)
    for mapped in (False,True):
        seen=0
        with reader.blocks(path,block,magic,mapped=mapped) as blocks:
            for data,pointer in blocks:
                assert isinstance(data,memoryview)==mapped
                positions=screen.select(data,math.inf,k=10,pointer=pointer)
                assert positions is not None
                for i in range(len(data)//fmt.size):
                    expected=math.fsum((a-b)*(a-b) for a,b in zip(vectors[seen+i],q))
                    actual=screen.squared(data,i,pointer=pointer)
                    assert struct.pack('<d',actual)==struct.pack('<d',expected)
                    checks+=1
                seen+=len(data)//fmt.size
        assert seen==13
    # Closing a partially consumed iterator releases its view and mapping.
    node=next(iter(reader.maps.values()))
    with reader.blocks(path,block,magic,mapped=True) as blocks:
        view,pointer=next(blocks);assert screen.squared(view,0,pointer=pointer) is not None
    assert next(iter(reader.maps.values())) is node
    try:len(view)
    except ValueError:pass
    else:assert False
    try:
        with reader.blocks(path,block,magic,mapped=True) as blocks:
            next(blocks);raise RuntimeError('consumer failure')
    except RuntimeError:pass
    assert hashlib.sha256(path.read_bytes()).digest()==before
    # Both writable and readonly non-bytes inputs obey the existing API.
    for data in (memoryview(payload),bytearray(payload)):
        assert screen.select(data,math.inf) is not None
        assert screen.squared(data,0) is not None
        if isinstance(data,memoryview):data.release()
    reader.clear()
    original=indexio.mmap.mmap
    def denied(*a,**kw):raise PermissionError('test-only denied mapping')
    indexio.mmap.mmap=denied
    with reader.blocks(path,block,magic,mapped=True) as blocks:
        assert b''.join(data for data,pointer in blocks)==payload
    indexio.mmap.mmap=original
    os.environ['MINIDB_INDEX_MMAP']='0'
    with reader.blocks(path,block,magic,mapped=True) as blocks:
        assert b''.join(data for data,pointer in blocks)==payload
    os.environ.pop('MINIDB_INDEX_MMAP')
    # Budget overflow uses buffered data; many tiny files exercise LRU bounds.
    saved=indexio.MAP_BYTES;indexio.MAP_BYTES=4096;reader.clear()
    if len(payload)+8>4096:
        with reader.blocks(path,block,magic,mapped=True) as blocks:
            assert all(pointer is None for data,pointer in blocks)
        assert not reader.maps
    indexio.MAP_BYTES=saved
    for i in range(20):
        small=Path('/data/small'+str(i));small.write_bytes(magic+b'abcdefgh')
        with reader.blocks(small,8,magic,mapped=True) as blocks:list(blocks)
    assert len(reader.maps)<=indexio.MAP_FILES and reader.bytes<=saved
    reader.clear();assert not reader.maps and reader.bytes==0
    indexio.MAP_BYTES=8192
    for i in range(5):
        with reader.blocks(Path('/data/small'+str(i)),8,magic,mapped=True) as blocks:list(blocks)
    assert len(reader.maps)==2 and reader.bytes==8192
    reader.clear();indexio.MAP_BYTES=saved
Path('/data/test.index').write_bytes(magic)
with reader.blocks(Path('/data/test.index'),65536,magic,mapped=True) as blocks:assert list(blocks)==[]
reader.clear()
print(json.dumps({'status':'PASS','reference_distance_checks':checks,'dimensions':[16,128,129,4096],'coverage':['mapped/buffered equality','bitwise exact sums','tail blocks','early close','consumer error cleanup','cached mapping reuse','file content unchanged','readonly buffer copy fallback','writable buffer borrowing','mapping denied fallback','environment opt-out','budget overflow buffered fallback','LRU eviction','empty index']}))
'''
if __name__=='__main__':
    r=subprocess.run(['docker','run','--rm','-i','--network=none','--read-only','--memory=64m','--memory-swap=64m','--tmpfs','/data:rw,size=4m','--entrypoint','python3','minivectordb:mmap-candidate','-B','-'],input=PROBE,text=True,capture_output=True,timeout=30)
    assert r.returncode==0,(r.stdout,r.stderr)
    out=Path(__file__).resolve().parents[1]/'protocol-results/index-mapping';out.mkdir(parents=True,exist_ok=True)
    (out/'result.json').write_text(r.stdout);print(r.stdout,end='')
