#!/usr/bin/env python3
"""Terminate at actual manifest replacement boundaries; production has no test hooks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = r'''
import os, sys
real_replace = os.replace
def replace(src, dst):
    armed = os.path.exists(os.path.join(sys.argv[1], 'armed'))
    target = os.path.basename(dst) == 'manifest.json'
    if armed and target and os.environ['CUT'] == 'before': os._exit(86)
    real_replace(src, dst)
    if armed and target and os.environ['CUT'] == 'after': os._exit(86)
os.replace = replace
from minidb.server import main
raise SystemExit(main())
'''


class Client:
    def __init__(self, data, cut=None):
        cmd = [sys.executable, '-B', '-u']
        cmd += ['-c', WRAPPER, str(data)] if cut else ['-m', 'minidb.server', str(data)]
        self.p = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, 'CUT': cut or ''},
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert json.loads(self.p.stdout.readline()) == {'type': 'ready'}

    def query(self, sql):
        self.p.stdin.write(json.dumps({'id': 1, 'op': 'execute', 'sql': sql}) + '\n'); self.p.stdin.flush()
        result = json.loads(self.p.stdout.readline())
        assert result['ok'], result
        return result

    def close(self):
        self.p.stdin.close(); self.p.wait(timeout=10)
        err = self.p.stderr.read()
        assert self.p.returncode == 0 and not err, (self.p.returncode, err)


def main():
    results = []
    for cut in ('before', 'after'):
        for explicit in (False, True):
            with tempfile.TemporaryDirectory(prefix='rewrite-recovery-') as d:
                data = Path(d)
                c = Client(data, cut)
                for table in ('a', 'b'):
                    c.query(f'CREATE TABLE {table}(id BIGINT PRIMARY KEY,n BIGINT,e VECTOR(2))')
                    c.query(f"INSERT INTO {table} VALUES(1,0,'[0,0]'),(2,0,'[1,1]')")
                    c.query(f'CREATE VECTOR INDEX ix ON {table}(e)')
                if explicit:
                    c.query('BEGIN')
                    c.query("UPDATE a SET n=7,e='[9,9]'")
                    c.query('DELETE FROM b WHERE id=1')
                    c.query("INSERT INTO b VALUES(3,7,'[9,9]')")
                    statement = 'COMMIT'
                else:
                    statement = "INSERT INTO a VALUES " + ','.join(f"({i},7,'[9,9]')" for i in range(3, 1003))
                (data / 'armed').touch()
                c.p.stdin.write(json.dumps({'id': 2, 'op': 'execute', 'sql': statement}) + '\n'); c.p.stdin.flush()
                assert c.p.stdout.readline() == ''
                c.p.wait(timeout=10)
                assert c.p.returncode == 86
                (data / 'armed').unlink()
                for repetition in range(2):
                    r = Client(data)
                    a = r.query('SELECT id,n FROM a ORDER BY id')['rows']
                    b = r.query('SELECT id,n FROM b ORDER BY id')['rows']
                    if cut == 'before':
                        assert a == b == [[1, 0], [2, 0]], (a[:3], b)
                    elif explicit:
                        assert a == [[1, 7], [2, 7]] and b == [[2, 0], [3, 7]], (a, b)
                    else:
                        assert a == [[1, 0], [2, 0]] + [[i, 7] for i in range(3, 1003)]
                        assert b == [[1, 0], [2, 0]]
                    top = r.query("SELECT id FROM a ORDER BY L2_DISTANCE(e,'[0,0]') LIMIT 2")['rows']
                    assert top == [[1], [2]], top
                    r.close()
                results.append({'cut': cut, 'explicit_multitable': explicit, 'two_restarts': 'PASS'})
    # An interrupted statement without COMMIT must leave the committed catalog untouched.
    with tempfile.TemporaryDirectory() as d:
        c = Client(d)
        c.query('CREATE TABLE t(id BIGINT PRIMARY KEY,n BIGINT)')
        c.query('INSERT INTO t VALUES(1,0)')
        c.query('BEGIN'); c.query('UPDATE t SET n=9')
        c.p.kill(); c.p.wait(timeout=10)
        r = Client(d)
        assert r.query('SELECT * FROM t')['rows'] == [[1, 0]]
        r.close()
        results.append({'kill_without_commit': 'PASS'})
    out = ROOT / 'protocol-results/recovery.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps({'status': 'PASS', 'scenarios': len(results)}))


if __name__ == '__main__': main()
