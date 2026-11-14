"""External observer. Missing peak/I/O remains null, never replaced by current/RSS."""
import ctypes
import json
import os
from pathlib import Path
import sys
import time
import traceback

pid=int(sys.argv[1]);members=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
v2=next((x[3:] for x in members if x.startswith('0::')),None)
root=Path('/host-cgroup')
def controller(name):
    for line in members:
        _,controllers,relative=line.split(':',2)
        if name in controllers.split(','):
            for folder in (name,controllers,'cpu,cpuacct','cpuacct,cpu'):
                path=root/folder/relative.lstrip('/')
                if path.exists():return path
    return None
groups={'memory':controller('memory'),'cpu':controller('cpuacct'),'io':controller('blkio')}
group=root/v2.lstrip('/') if v2 is not None else groups['memory']
device=os.stat('/observe-data').st_dev;device=f'{os.major(device)}:{os.minor(device)}'
def read(path):
    try:return path.read_text().strip() if path else None
    except OSError:return None
def integer(path):
    value=read(path)
    try:return int(value) if value is not None else None
    except ValueError:return None
def pairs(path):
    text=read(path)
    return {k:int(v) for k,v in (x.split() for x in text.splitlines())} if text else {}
def snapshot():
    io={}
    if v2 is not None:
        text=read(group/'io.stat')
        if text:
            for line in text.splitlines():
                fields=line.split();io[fields[0]]={k:int(v) for k,v in (x.split('=') for x in fields[1:])}
        peak=integer(group/'memory.peak');current=integer(group/'memory.current');stat=pairs(group/'memory.stat');cpu=pairs(group/'cpu.stat');events=pairs(group/'memory.events')
        limits={n:read(group/n) for n in ('cpu.max','memory.max','memory.swap.max','pids.max','io.max')}
    else:
        mem=groups['memory'];blk=groups['io'];cp=groups['cpu']
        text=read(blk/'blkio.throttle.io_service_bytes') if blk else None
        if text:
            for line in text.splitlines():
                fields=line.split()
                if len(fields)==3 and fields[1] in ('Read','Write'):io.setdefault(fields[0],{})['rbytes' if fields[1]=='Read' else 'wbytes']=int(fields[2])
        peak=integer(mem/'memory.max_usage_in_bytes') if mem else None;current=integer(mem/'memory.usage_in_bytes') if mem else None
        stat=pairs(mem/'memory.stat') if mem else {};events={};usage=integer(cp/'cpuacct.usage') if cp else None;cpu={'usage_nsec':usage};limits={}
    return {'cgroup':str(group),'cgroup_version':2 if v2 is not None else 1,'data_device':device,'io_by_device':io,'io_available':text is not None,
            'memory_peak_bytes':peak,'memory_current_bytes':current,'memory_stat':stat,'cpu_stat':cpu,'memory_events':events,'limits':limits,
            'files':{p.name:p.stat().st_size for p in Path('/observe-data').iterdir() if p.is_file()}}
print(json.dumps({'ready':True,'data_device':device,'cgroup':str(group)}),flush=True)
for line in sys.stdin:
    try:
        command=json.loads(line)
        if command.get('op')=='stop':break
        elapsed=0.
        if command.get('drain'):
            fd=os.open('/observe-data',os.O_RDONLY|os.O_DIRECTORY)
            try:
                libc=ctypes.CDLL(None,use_errno=True);start=time.perf_counter();result=libc.syncfs(fd);elapsed=time.perf_counter()-start
                if result:raise OSError(ctypes.get_errno(),'syncfs failed')
            finally:os.close(fd)
            previous=None;stable=0
            for _ in range(100):
                data=snapshot();dirty=sum(data['memory_stat'].get(k,0) for k in ('file_dirty','file_writeback','dirty','writeback'))
                stable=stable+1 if not dirty and data['io_by_device']==previous else 0
                if stable>=3:break
                previous=data['io_by_device'];time.sleep(.1)
            else:raise RuntimeError('writeback did not settle')
        data=snapshot();data['external_syncfs_seconds']=elapsed
        print(json.dumps({'ok':True,'snapshot':data}),flush=True)
    except Exception:print(json.dumps({'ok':False,'error':traceback.format_exc()}),flush=True)
