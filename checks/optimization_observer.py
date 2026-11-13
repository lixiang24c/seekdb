"""External cgroup observer: this process never joins the measured cgroup."""
import ctypes
import json
import os
from pathlib import Path
import sys
import time
import traceback

pid = int(sys.argv[1])
entry = next(x for x in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if x.startswith('0::'))
group = Path('/host-cgroup') / entry[3:].lstrip('/')
device = os.stat('/observe-data').st_dev
device = f'{os.major(device)}:{os.minor(device)}'


def pairs(name):
    return {k: int(v) for k, v in (x.split() for x in (group / name).read_text().splitlines())}


def snapshot():
    io = {}
    for line in (group / 'io.stat').read_text().splitlines():
        fields = line.split()
        io[fields[0]] = {k: int(v) for k, v in (x.split('=') for x in fields[1:])}
    return {'cgroup': str(group), 'data_device': device, 'io_by_device': io,
            'memory_peak_bytes': int((group / 'memory.peak').read_text()),
            'memory_current_bytes': int((group / 'memory.current').read_text()), 'index_file_bytes': sum(p.stat().st_size for p in Path('/observe-data').glob('*.index')),
            'memory_stat': pairs('memory.stat'), 'cpu_stat': pairs('cpu.stat'),
            'memory_events': pairs('memory.events'), 'memory_pressure': (group / 'memory.pressure').read_text(),
            'limits': {n: (group / n).read_text().strip()
                       for n in ('cpu.max', 'memory.max', 'memory.swap.max', 'pids.max', 'io.max')}}


print(json.dumps({'ready': True, 'data_device': device, 'cgroup': str(group)}), flush=True)
for line in sys.stdin:
    try:
        command = json.loads(line)
        if command.get('op') == 'stop':
            break
        sync_seconds = 0.0
        if command.get('drain'):
            # syncfs is issued from the separate observer cgroup. Writeback remains
            # attributed to the database that dirtied the data, where supported.
            fd = os.open('/observe-data', os.O_RDONLY | os.O_DIRECTORY)
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                start = time.perf_counter()
                result = libc.syncfs(fd)
                sync_seconds = time.perf_counter() - start
                if result:
                    raise OSError(ctypes.get_errno(), 'syncfs failed')
            finally:
                os.close(fd)
            previous, stable = None, 0
            for attempt in range(100):
                current = snapshot()
                dirty = current['memory_stat'].get('file_dirty', 0) + current['memory_stat'].get('file_writeback', 0)
                if not dirty and current['io_by_device'] == previous:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                previous = current['io_by_device']
                time.sleep(0.1)
            else:
                raise RuntimeError('writeback counters did not settle within 10 seconds')
        result = snapshot()
        result['external_syncfs_seconds'] = sync_seconds
        print(json.dumps({'ok': True, 'snapshot': result}), flush=True)
    except Exception:
        print(json.dumps({'ok': False, 'error': traceback.format_exc()}), flush=True)
