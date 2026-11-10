"""Local SQL-process driver. This module defines the local adapter, not an official protocol."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, data, logs, image=None, memory='128m', timeout=30, mode='exact'):
        self.data = Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        logs = Path(logs)
        logs.mkdir(parents=True, exist_ok=True)
        self.mode=mode;self.sequence=0;self.jsonl=False
        self.timeout = timeout
        self.name = 'minivector-eval-' + uuid.uuid4().hex if image else None
        self.stderr = (logs / 'stderr.log').open('a')
        self.transcript = (logs / 'transcript.jsonl').open('a')
        self.pid = None
        if image:
            command = ['docker', 'run', '--rm', '--name', self.name, '--network', 'none',
                       '--cpus', '2', '--memory', memory, '--memory-swap', memory,
                       '-i', '-v', str(self.data.resolve()) + ':/data', image, '--mode', mode]
        else:
            command = [sys.executable, '-u', '-m', 'minivectordb.server', '--data', str(self.data), '--mode', mode]
        self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1)
        self.messages = queue.Queue()
        def reader():
            try:
                for line in self.process.stdout:
                    self.messages.put(line)
            finally:
                self.messages.put(None)
        self.reader = threading.Thread(target=reader, daemon=True)
        self.reader.start()
        try:
            ready = self.read()
            self.jsonl=ready!='LOCAL_SQL_READY\n'
            if self.jsonl and json.loads(ready)!={'type':'ready'}:
                raise AssertionError('invalid readiness: ' + repr(ready))
            if self.name:
                self.pid = int(subprocess.check_output(['docker', 'inspect', '-f', '{{.State.Pid}}', self.name], text=True))
        except BaseException:
            self.close(kill=True)
            raise

    def read(self):
        try:
            line = self.messages.get(timeout=self.timeout)
        except queue.Empty:
            raise TimeoutError('database response timeout')
        if line is None:
            raise RuntimeError('database exited, code=' + str(self.process.poll()))
        return line

    def execute(self, sql, error=False):
        started = time.perf_counter()
        if self.jsonl:
            self.sequence+=1
            request={'id':self.sequence,'op':'execute','sql':sql,'search_mode':self.mode}
            self.process.stdin.write(json.dumps(request,ensure_ascii=False,allow_nan=False)+'\n')
        else:self.process.stdin.write(sql.rstrip().rstrip(';') + ';\n')
        self.process.stdin.flush()
        rows = []
        columns = None
        while True:
            message = json.loads(self.read())
            if self.jsonl:
                if type(message.get('id')) is not int or message['id']!=self.sequence or type(message.get('ok')) is not bool:raise RuntimeError('invalid response id/ok')
                rows=message.get('rows',[])
            if 'columns' in message:
                columns = message['columns']
            if 'row' in message:
                rows.append(message['row'])
            if 'ok' in message:
                self.transcript.write(json.dumps({'sql': sql, 'reply': message, 'rows': rows,
                                                  'columns': columns, 'seconds': time.perf_counter()-started},
                                                 ensure_ascii=False, allow_nan=False) + '\n')
                self.transcript.flush()
                if bool(message['ok']) == bool(error):
                    raise AssertionError('unexpected SQL status: ' + repr(message) + '\n' + sql[:500])
                return rows

    def metrics(self):
        """Read cgroup v2 from the host; no sampler is started in the measured container."""
        if not self.pid:
            return None
        membership = Path('/proc') / str(self.pid) / 'cgroup'
        relative = next(line.split('::', 1)[1] for line in membership.read_text().splitlines() if line.startswith('0::'))
        group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        # Docker may run in a separate cgroup namespace: fail explicitly instead of inventing zeros.
        peak = int((group / 'memory.peak').read_text())
        io = {}
        for line in (group / 'io.stat').read_text().splitlines():
            device, *pairs = line.split()
            io[device] = {key:int(value) for key, value in (pair.split('=') for pair in pairs)}
        return {'memory_peak_bytes': peak, 'io_by_device': io,
                'io_bytes': sum(v.get('rbytes',0)+v.get('wbytes',0) for v in io.values()),
                'scope': 'whole local container cgroup; all devices, not an official dedicated data device'}

    def close(self, kill=False):
        process = self.process
        try:
            if kill and self.name:
                subprocess.run(['docker', 'kill', '--signal', 'KILL', self.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if process.poll() is None:
                if kill:
                    process.kill()
                elif process.stdin and not process.stdin.closed:
                    if self.jsonl:
                        self.sequence+=1
                        process.stdin.write(json.dumps({'id':self.sequence,'op':'shutdown'})+'\n');process.stdin.flush()
                        reply=json.loads(self.read())
                        if reply.get('id')!=self.sequence or reply.get('ok') is not True:raise RuntimeError('invalid shutdown response')
                    process.stdin.close()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if self.name:
                subprocess.run(['docker','rm','-f',self.name], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
            self.reader.join(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:
                    stream.close()
            self.stderr.close()
            self.transcript.close()
