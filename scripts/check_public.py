#!/usr/bin/env python3
"""Run upstream public SQL assertions through the local stdin/stdout adapter."""
import argparse
import json
import math
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def matches(actual,expected):
    if type(expected) is float:
        return type(actual) in (int,float) and math.isfinite(actual) and math.isclose(actual,expected,rel_tol=1e-5,abs_tol=1e-5)
    if type(actual) is not type(expected):return False
    if isinstance(expected,list):return len(actual)==len(expected) and all(matches(a,b) for a,b in zip(actual,expected))
    return actual==expected


def check_reply(actual,expected):
    for key,value in expected.items():
        if key in ('unordered','rows'):continue
        if key not in actual or not matches(actual[key],value):
            raise AssertionError(f'{key}: expected {value!r}, got {actual.get(key)!r}')
    if 'rows' not in expected:return
    remaining=list(actual.get('rows',[]))
    if expected.get('unordered'):
        for row in expected['rows']:
            found=next((i for i,candidate in enumerate(remaining) if matches(candidate,row)),None)
            if found is None:raise AssertionError(f'missing row: {row!r}')
            remaining.pop(found)
        if remaining:raise AssertionError(f'unexpected rows: {remaining!r}')
    elif not matches(remaining,expected['rows']):
        raise AssertionError(f'rows: expected {expected["rows"]!r}, got {remaining!r}')


def run_case(root,case,data_root):
    statements=[line.strip() for line in (root/'cases'/(case['id']+'.test')).read_text(encoding='utf-8').splitlines() if line.strip()]
    expected=json.loads((root/'cases'/(case['id']+'.result')).read_text(encoding='utf-8'))
    if len(statements)!=case['statements'] or len(expected)!=len(statements):raise AssertionError('case/manifest statement count mismatch')
    with tempfile.TemporaryDirectory(prefix=case['id']+'-',dir=data_root) as directory:
        process=subprocess.Popen([sys.executable,'-u','-m','minivectordb','--data',directory],
                                 stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        messages=queue.Queue()
        def reader():
            try:
                for line in process.stdout:messages.put(line)
            finally:messages.put(None)
        thread=threading.Thread(target=reader,daemon=True);thread.start()
        def read():
            line=messages.get(timeout=20)
            if line is None:raise RuntimeError(f'database exited: {process.poll()}')
            return line
        try:
            if read()!='LOCAL_SQL_READY\n':raise AssertionError('unexpected local readiness')
            for number,(sql,wanted) in enumerate(zip(statements,expected),1):
                process.stdin.write(sql.rstrip(';')+';\n');process.stdin.flush();actual={};rows=[]
                while True:
                    frame=json.loads(read())
                    if 'columns' in frame:actual['columns']=frame['columns'];actual['rows']=rows
                    if 'row' in frame:rows.append(frame['row'])
                    if 'ok' in frame:
                        if frame['ok'] is not True:raise AssertionError(f'{case["id"]} SQL {number}: {frame}')
                        result=frame.get('result')
                        if isinstance(result,dict) and 'affected' in result:actual['affected_rows']=result['affected']
                        break
                try:check_reply(actual,wanted)
                except AssertionError as error:raise AssertionError(f'{case["id"]} SQL {number}: {sql}\n{error}') from error
        finally:
            process.stdin.close()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait();raise
            thread.join(timeout=2);process.stdout.close()
        if process.returncode:raise AssertionError(f'database exit code: {process.returncode}')
    return len(statements)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,default=Path('mysqltest'))
    parser.add_argument('--data-root',type=Path,default=Path('/data'))
    parser.add_argument('--case')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args();args.data_root.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((args.cases/'manifest.json').read_text(encoding='utf-8'))
    cases=[case for case in manifest['cases'] if not args.case or args.case==case['id']]
    if not cases:parser.error('unknown case')
    report={'scope':'upstream public SQL cases through local process adapter','version':manifest['version'],'cases':[],'status':'PASS'}
    for case in cases:
        try:
            count=run_case(args.cases,case,args.data_root)
            report['cases'].append({'id':case['id'],'statements':count,'status':'PASS'})
        except Exception as error:
            report['status']='FAIL';report['cases'].append({'id':case['id'],'status':'FAIL','error':str(error)})
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
