#!/usr/bin/env python3
"""Regression checks for ASCII keywords and JSON decoder failure recovery."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import json
from minidb.sql import Parser, DBError

checks = 0
def rejects(text):
    global checks
    try:
        Parser(text).parse()
    except DBError as exc:
        assert exc.code == 'unsupported_sql', (text[:60], exc.code)
    else:
        raise AssertionError(('accepted invalid SQL', text[:60]))
    checks += 1

for keyword in ('İNSERT', 'ıNSERT', 'INſERT'):
    rejects(keyword + ' INTO t VALUES (1,[0])')
for keyword in ('İNTO', 'ıNTO'):
    rejects('INSERT ' + keyword + ' t VALUES (1,[0])')
rejects('INSERT INTO t VALUEſ (1,[0])')
for space in (' ', '\t', '\n', '\x1c', '\u00a0', '\u2003'):
    text = space + 'iNsErT' + space + 'iNtO' + space + 't' + space + 'vAlUeS (1,[0])'
    assert Parser(text).parse() == {'op':'insert','table':'t','cols':None,'rows':[[1,[0]]]}
    checks += 1
for depth in (1100, 3000):
    bad = '[{"a":' + '[' * depth + '0' + ']' * depth + '}]'
    for template in ('INSERT INTO t VALUES (1,{})', 'INSERT INTO t(id,v) VALUES (1,{})',
                     'UPDATE t SET v={} WHERE id=1', 'SELECT id FROM t ORDER BY L2_DISTANCE(v,{}) LIMIT 1'):
        rejects(template.format(bad))
print(json.dumps({'status':'PASS','parser_checks':checks}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', help='Test the built image; otherwise use the host source.')
    args = parser.parse_args()
    if args.image:
        base = ['docker', 'run', '--rm', '-i', '--network=none', '--cpus=2', '--memory=2g',
                '--memory-swap=2g', '--pids-limit=128', '--read-only', '--cap-drop=ALL',
                '--security-opt=no-new-privileges', '--tmpfs', '/data:rw,size=16m']
        command = base + ['--entrypoint=python3', args.image, '-B', '-']
    else:
        command = [sys.executable, '-B', '-']
    probe = subprocess.run(command, cwd=ROOT, input=PROBE, text=True, capture_output=True, timeout=60)
    if probe.returncode:
        raise RuntimeError(probe.stdout + probe.stderr)
    print(probe.stdout.strip())

    process_checks = 0
    bad = '[{"a":' + '[' * 1100 + '0' + ']' * 1100 + '}]'
    invalid_sql = ['INSERT INTO t VALUES (2,' + bad + ')',
                   'INSERT INTO t(id,v) VALUES (2,' + bad + ')',
                   'İNSERT INTO t VALUES (2,[0])', 'INSERT INTO t VALUEſ (2,[0])']
    for sql in invalid_sql:
        for ending in ('COMMIT', 'ROLLBACK'):
            statements = ['CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(1))', 'BEGIN',
                          'INSERT INTO t VALUES (1,[1])', sql, 'SELECT id FROM t ORDER BY id',
                          'INSERT INTO t VALUES (3,[3])', ending, 'SELECT id FROM t ORDER BY id']
            requests = [{'id': i, 'op': 'execute', 'sql': text} for i, text in enumerate(statements)]
            requests.append({'id': 8, 'op': 'shutdown'})
            wire = ''.join(json.dumps(req) + '\n' for req in requests)
            with tempfile.TemporaryDirectory() as data:
                command = base + [args.image] if args.image else [sys.executable, '-B', '-m', 'minidb.server', data]
                result = subprocess.run(command, cwd=ROOT, input=wire, text=True, capture_output=True, timeout=60)
            assert result.returncode == 0, (sql[:60], result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            assert replies[0] == {'type':'ready'} and len(replies) == 10, replies
            by_id = {r['id']: r for r in replies[1:]}
            assert by_id[3]['ok'] is False and by_id[3]['error']['code'] == 'unsupported_sql', by_id[3]
            assert all(by_id[i]['ok'] for i in (0, 1, 2, 4, 5, 6, 7, 8)), by_id
            assert by_id[4]['rows'] == [[1]], by_id[4]
            assert by_id[7]['rows'] == ([[1],[3]] if ending == 'COMMIT' else []), by_id[7]
            process_checks += 1
    print(json.dumps({'status':'PASS','process_transaction_checks':process_checks}))


if __name__ == '__main__':
    main()
