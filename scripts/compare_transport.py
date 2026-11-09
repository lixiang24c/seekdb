#!/usr/bin/env python3
"""Short paired SQL-process comparison of output transport implementations."""
import argparse
import contextlib
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from evaluation.benchmark import prepare
from minivectordb.core import Database
from minivectordb.sql import SQL

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline-server',required=True)
parser.add_argument('--baseline-sql')
parser.add_argument('--baseline-core')
parser.add_argument('--output',required=True)
args=parser.parse_args()
baseline=Path(args.baseline_server).resolve()
vectors,_,requests,truth=prepare(720,10000,128,200)
requests=requests*3;truth=truth*3

@contextlib.contextmanager
def session(directory,old):
    if old:
        code='import sys;from minivectordb import server;exec(compile(open(sys.argv.pop(1)).read(),"baseline-server","exec"),server.__dict__);server.main()'
        command=[sys.executable,'-u','-c',code,str(baseline),'--data',directory]
        if args.baseline_sql:
            code='import sys;from minivectordb import sql;exec(compile(open(sys.argv.pop(1)).read(),"baseline-sql","exec"),sql.__dict__);'+code
            command=[sys.executable,'-u','-c',code,str(Path(args.baseline_sql).resolve()),str(baseline),'--data',directory]
        if args.baseline_core:
            code='import sys;from minivectordb import core;exec(compile(open(sys.argv.pop(1)).read(),"baseline-core","exec"),core.__dict__);'+code
            command=[sys.executable,'-u','-c',code,str(Path(args.baseline_core).resolve())]+command[4:]
    else:
        command=[sys.executable,'-u','-m','minivectordb.server','--data',directory]
    process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,text=True)
    def execute(statement):
        process.stdin.write(statement+';\n');process.stdin.flush();rows=[]
        while True:
            line=process.stdout.readline()
            if not line:raise RuntimeError('database exited before reply')
            frame=json.loads(line)
            if 'row' in frame:rows.append(frame['row'])
            if 'ok' in frame:
                if not frame['ok']:raise AssertionError(frame)
                return rows
    try:
        assert process.stdout.readline()=='LOCAL_SQL_READY\n'
        yield execute
    finally:
        process.stdin.close()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait();raise
        process.stdout.close();process.stderr.close()

with tempfile.TemporaryDirectory(prefix='minivector-transport-') as directory:
    db=Database(directory);sql=SQL(db)
    sql.execute('CREATE TABLE t(id BIGINT PRIMARY KEY, tag BIGINT, v VECTOR(128))')
    db.insert('t',({'id':i+1,'tag':(i+1)%10,'v':vector.tolist()} for i,vector in enumerate(vectors)))
    sql.execute('CREATE VECTOR INDEX ix ON t(v)');db.close()
    before=[];after=[]
    with session(directory,True) as old,session(directory,False) as new:
        for statement in requests[:50]:old(statement);new(statement)
        for iteration in range(3):
            elapsed=[0.,0.];answers=[[],[]]
            for start in range(0,len(requests),20):
                order=(1,0) if (start//20+iteration)%2 else (0,1)
                for version in order:
                    execute=(old,new)[version]
                    started=time.perf_counter()
                    block=[execute(statement) for statement in requests[start:start+20]]
                    elapsed[version]+=time.perf_counter()-started
                    answers[version].extend(block)
            for replies in answers:
                for actual,expected in zip(replies,truth):
                    assert [row[0] for row in actual]==[row[0] for row in expected]
                    assert all(math.isclose(a[1],b[1],rel_tol=1e-6,abs_tol=1e-6) for a,b in zip(actual,expected))
            before.append(elapsed[0]);after.append(elapsed[1])
report={'scope':'local SQL processes over stdin/stdout, same storage and native kernel; 3 rounds of alternating 20-query blocks',
        'rows':10000,'dimensions':128,'queries':len(requests),'unique_queries':200,'k':10,'seed':720,
        'filtered_fraction':0.5,'exact_ids_order_distances_correct':True,
        'baseline_server_sha256':hashlib.sha256(baseline.read_bytes()).hexdigest(),
        'candidate_server_sha256':hashlib.sha256((ROOT/'minivectordb/server.py').read_bytes()).hexdigest(),
        'baseline_sql_sha256':hashlib.sha256(Path(args.baseline_sql).read_bytes()).hexdigest() if args.baseline_sql else None,
        'candidate_sql_sha256':hashlib.sha256((ROOT/'minivectordb/sql.py').read_bytes()).hexdigest(),
        'baseline_core_sha256':hashlib.sha256(Path(args.baseline_core).read_bytes()).hexdigest() if args.baseline_core else None,
        'candidate_core_sha256':hashlib.sha256((ROOT/'minivectordb/core.py').read_bytes()).hexdigest(),
        'baseline_rounds_seconds':before,'candidate_rounds_seconds':after,
        'baseline_median_seconds':statistics.median(before),'candidate_median_seconds':statistics.median(after),
        'speedup':statistics.median(before)/statistics.median(after)}
Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
