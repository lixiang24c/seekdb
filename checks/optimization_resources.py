import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import selectors
import statistics
import subprocess
import sys
import time
import traceback
from _transport import Session

def docker(*args, timeout=60, check=True):
    result = subprocess.run(['docker', *map(str, args)], capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace'))
    return result

class Observer:

    def __init__(self, session):
        inspected = json.loads(docker('inspect', session.name).stdout)[0]
        self.name = session.name + '-observer'
        (OUT / (session.name + '.inspect.json')).write_text(json.dumps(inspected, indent=2))
        pid = inspected['State']['Pid']
        self.err = (OUT / (self.name + '.stderr.log')).open('wb')
        argv = ['docker', 'run', '--rm', '--name', self.name, '-i', '--network=none', '--cpus=1', '--memory=128m', '--memory-swap=128m', '--pids-limit=32', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pid=host', '--cgroupns=host', '--mount', 'type=bind,src=/sys/fs/cgroup,dst=/host-cgroup,readonly', '--mount', f'type=volume,src={session.volume},dst=/observe-data,readonly', '--entrypoint', 'python3', IMAGE, '-B', '-u', '-c', (HERE / 'optimization_observer.py').read_text(), str(pid)]
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.p.stdout, selectors.EVENT_READ)
        try:
            ready = self.read()
            assert ready.get('ready'), ready
        except BaseException:
            self.close()
            raise

    def read(self):
        if not self.selector.select(60):
            raise TimeoutError('external observer timeout')
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError('external observer exited; see observer stderr log')
        return json.loads(line)

    def snapshot(self, drain=False):
        self.p.stdin.write((json.dumps({'op': 'snapshot', 'drain': drain}) + '\n').encode())
        self.p.stdin.flush()
        result = self.read()
        assert result.get('ok'), result
        result = result['snapshot']
        limits = result['limits']
        quota, period = map(int, limits['cpu.max'].split())
        assert quota == float(CONFIG['cpus']) * period and int(limits['memory.max']) == MEMORY_LIMIT
        assert limits['memory.swap.max'] == '0' and limits['pids.max'] == '128'
        return result

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            try:
                self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                docker('rm', '-f', self.name, check=False)
                self.p.wait(timeout=10)
        self.p.stdout.close()
        self.selector.close()
        self.err.close()

def data_bytes(snapshot):
    item = snapshot['io_by_device'].get(snapshot['data_device'])
    return None if item is None else item.get('rbytes', 0) + item.get('wbytes', 0)
