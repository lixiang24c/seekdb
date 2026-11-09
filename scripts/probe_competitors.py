#!/usr/bin/env python3
"""Isolated numerical regressions and a targeted cross-table WAL crash probe."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from evaluation.stress_run import Process,check,decode_text

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--teammate-bin',required=True);parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
binary=str(Path(args.teammate_bin).resolve())
subprocess.run([os.environ.get('CC','gcc'),'-shared','-fPIC','-O2',str(ROOT/'tests/cut_second_wal.c'),'-o',str(root/'cut_second_wal.so'),'-ldl'],check=True)
cases=[
 ('quoted_vector',['CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))',"INSERT INTO t VALUES(1,'[3,4]')"],[{}, {'affected_rows':1}]),
 ('distance_projection',['CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))','INSERT INTO t VALUES(1,[3,4]),(2,[0,0])','SELECT id,L2_DISTANCE(v,[0,0]) AS d FROM t ORDER BY d LIMIT 2'],[{}, {'affected_rows':2},{'columns':['id','d'],'rows':[[2,0.],[1,5.]]}]),
 ('finite_overflow_order',['CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))','INSERT INTO t VALUES(1,[3e38,0]),(2,[2e38,0])','SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2'],[{}, {'affected_rows':2},{'rows':[[2],[1]]}]),
 ('tiny_underflow_order',['CREATE TABLE t(id BIGINT PRIMARY KEY,v VECTOR(2))','INSERT INTO t VALUES(1,[2e-30,0]),(2,[1e-30,0])','SELECT id FROM t ORDER BY L2_DISTANCE(v,[0,0]) LIMIT 2'],[{}, {'affected_rows':2},{'rows':[[2],[1]]}])]
results={'scope':'numerical probes use bare vectors to isolate distance errors from quoted-input failures'}
for version,command in [('ours',[sys.executable,'-u','-m','minivectordb','--data','{data}']),('teammate',[binary,'{data}'])]:
    outcomes=[]
    for name,sqls,expected in cases:
        with tempfile.TemporaryDirectory(prefix=version+'-',dir=root) as data,(root/(version+'-'+name+'.stderr')).open('w') as log:
            process=Process(command,data,log,'json' if version=='ours' else 'text');result={'name':name,'status':'PASS'}
            try:
                for sql,wanted in zip(sqls,expected):
                    actual=process.execute(sql)
                    try:check(actual,wanted)
                    except AssertionError as error:
                        result.update(status='FAIL',sql=sql,expected=wanted,actual=actual,error=str(error));break
            finally:process.stop()
            outcomes.append(result)
    results[version]=outcomes
with tempfile.TemporaryDirectory(prefix='cross-table-',dir=root) as data,(root/'cross-table.stderr').open('w') as log:
    marker=Path(data)/'armed';environment=os.environ.copy()
    environment.update(LD_PRELOAD=str(root/'cut_second_wal.so'),MVDB_CUT_ARM=str(marker))
    process=Process([binary,'{data}'],data,log,'text',env=environment)
    setup=['CREATE TABLE a(id BIGINT PRIMARY KEY,n BIGINT)','CREATE TABLE b(id BIGINT PRIMARY KEY,n BIGINT)',
           'INSERT INTO a VALUES(1,0)','INSERT INTO b VALUES(1,0)','BEGIN',
           'UPDATE a SET n=1 WHERE id=1','UPDATE b SET n=1 WHERE id=1']
    for sql in setup:
        response=process.execute(sql)
        if response.get('error'):raise AssertionError((sql,response))
    marker.write_text('armed')
    try:process.execute('COMMIT');observed='commit returned'
    except RuntimeError as error:observed=str(error)
    process.stop(kill=True);exit_code=process.process.returncode;marker.unlink()
    reopened=Process([binary,'{data}'],data,log,'text');state=[]
    try:
        for table in ('a','b'):
            response=reopened.execute('SELECT n FROM '+table);decode_text(response,{'rows':[[0]]})
            state.append(response['rows'][0][0])
    finally:reopened.stop()
    results['teammate_cross_table_cut']={'setup':setup,'observed':observed,'exit_code':exit_code,
        'recovered_state':state,'allowed_states':[[0,0],[1,1]],'atomic':state in ([0,0],[1,1]),
        'fault':'SIGKILL before second table WAL append, after first table WAL fdatasync; no COMMIT success received'}
(root/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(results,ensure_ascii=False,indent=2))
