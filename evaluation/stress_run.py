"""Replay generated expectations against SQL processes, with protocol-only adapters."""
import gzip
import json
import math
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from .stress_model import f32


class Process:
    def __init__(self,command,data,stderr,protocol='json',cwd=None,timeout=15,env=None):
        self.command=[part.replace('{data}',str(data)) for part in command]
        self.stderr=stderr;self.protocol=protocol;self.cwd=cwd;self.timeout=timeout;self.env=env
        self.mode='exact'
        self.start()
    def start(self):
        self.process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=self.stderr,text=True,cwd=self.cwd,env=self.env)
        self.messages=queue.Queue()
        stream=self.process.stdout;messages=self.messages
        def reader():
            try:
                for line in stream:messages.put(line)
            finally:messages.put(None)
        self.thread=threading.Thread(target=reader,daemon=True);self.thread.start()
        if self.protocol=='json' and self.read()!='LOCAL_SQL_READY\n':raise RuntimeError('unexpected readiness')
        if self.mode!='exact':self.set_mode(self.mode)
    def read(self):
        line=self.messages.get(timeout=self.timeout)
        if line is None:raise RuntimeError('database exited: '+str(self.process.poll()))
        return line
    def execute(self,sql):
        self.process.stdin.write(sql.rstrip(';')+';\n');self.process.stdin.flush()
        actual={'rows':[]}
        if self.protocol=='text':
            while True:
                line=self.read().rstrip('\n')
                if line.startswith('ERROR '):return {'error':True,'message':line}
                if line=='OK' or line.startswith('OK '):
                    count=0 if line=='OK' else int(line.split()[1])
                    if sql.lstrip().upper().startswith('SELECT'):
                        if count!=len(actual['rows']):raise AssertionError('text row count mismatch')
                    else:actual['affected_rows']=count
                    actual['text_rows']=True
                    return actual
                actual['rows'].append(line.split('\t'))
        while True:
            frame=json.loads(self.read())
            if 'columns' in frame:actual['columns']=frame['columns']
            if 'row' in frame:actual['rows'].append(frame['row'])
            if 'ok' in frame:
                if frame['ok'] is not True:return {'error':True,'message':frame.get('error'),'error_code':frame.get('error_code')}
                result=frame.get('result')
                if isinstance(result,dict) and 'affected' in result:actual['affected_rows']=result['affected']
                return actual
    def stop(self,kill=False):
        process=self.process
        if process.poll() is None and kill:process.kill()
        try:process.stdin.close()
        except BrokenPipeError:pass
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait();raise
        self.thread.join(timeout=2);process.stdout.close()
        if not kill and process.returncode:raise RuntimeError('abnormal normal exit: '+str(process.returncode))
    def restart(self,kill=False):self.stop(kill);self.start()
    def set_mode(self,mode):
        if mode not in ('exact','ann'):raise ValueError('unknown mode')
        result=self.execute('--mode '+mode)
        if result.get('error'):raise AssertionError('mode switch rejected: '+str(result))
        self.mode=mode


def equal(actual,expected):
    if isinstance(expected,list):return isinstance(actual,list) and len(actual)==len(expected) and all(equal(a,b) for a,b in zip(actual,expected))
    if type(expected) is float:return type(actual) in (int,float) and math.isfinite(actual) and math.isclose(actual,expected,rel_tol=1e-5,abs_tol=1e-5)
    return type(actual) is type(expected) and actual==expected


def decode_text(actual,expected):
    rows=actual['rows'];sample=next(iter(expected.get('rows',[])),None)
    if not sample:
        if rows:raise AssertionError('unexpected rows for empty reference result')
        return
    def decode(value,example):
        if isinstance(example,str):return value
        if isinstance(example,list):return [f32(number) for number in json.loads(value)]
        if type(example) is int:return int(value)
        return float(value)
    converted=[]
    for row in rows:
        if len(row)!=len(sample):raise AssertionError('text field count mismatch')
        converted.append([decode(value,example) for value,example in zip(row,sample)])
    actual['rows']=converted


def check(actual,expected):
    if bool(actual.get('error'))!=bool(expected.get('error')):raise AssertionError('success/error mismatch')
    if expected.get('error'):
        if isinstance(expected['error'],str) and not actual.get('error_code'):raise AssertionError('expected nonempty error code')
        return
    if 'affected_rows' in expected and actual.get('affected_rows')!=expected['affected_rows']:raise AssertionError('affected_rows mismatch')
    if 'columns' in expected and not actual.get('text_rows') and actual.get('columns')!=expected['columns']:raise AssertionError('columns missing/mismatch')
    if 'rows' not in expected:return
    if actual.get('text_rows'):decode_text(actual,expected)
    def row_equal(left,right):
        return isinstance(left,list) and len(left)==len(right) and all(
            a==b if isinstance(b,list) else equal(a,b) for a,b in zip(left,right))
    if expected.get('unordered'):
        remaining=list(actual['rows'])
        for row in expected['rows']:
            index=next((i for i,other in enumerate(remaining) if row_equal(other,row)),None)
            if index is None:raise AssertionError('unordered row missing')
            remaining.pop(index)
        if remaining:raise AssertionError('unexpected unordered row')
    elif len(actual['rows'])!=len(expected['rows']) or not all(row_equal(a,b) for a,b in zip(actual['rows'],expected['rows'])):raise AssertionError('rows/order/value mismatch')


def run(suite,output,command,protocol='json',cwd=None,case_filter=None,data_root=None):
    import shutil
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    report={'version':'minivectordb-stateful-v1','scope':'local SQL-process differential correctness; elapsed is not benchmark T',
            'protocol':protocol,'columns_verified':protocol=='json','suite':str(suite),'command':command,
            'status':'RUNNING','sql_attempted':0,'sql_completed':0,'sql_passed':0,'sql_failed':0,'restarts':0,'cases':[]}
    started=time.monotonic();process=None;log=None;current=None
    def save():
        report['elapsed_seconds']=time.monotonic()-started
        temporary=output/'report.tmp';temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');temporary.replace(output/'report.json')
    def failure(record,error,actual=None):
        current['failed']+=1;report['sql_failed']+=1
        if 'first_failure' not in current:
            current['first_failure']={'record':ordinal,'sql':record.get('sql','')[:2048],
                                      'category':record.get('category'),'error':str(error),
                                      'expected':str(record.get('expected'))[:2048],'actual':str(actual)[:2048]}
    save();skip=False
    try:
        with gzip.open(suite,'rt',encoding='utf-8') as records:
            for ordinal,line in enumerate(records,1):
                record=json.loads(line);kind=record['kind']
                if kind=='case':
                    if not re.fullmatch(r'[A-Za-z0-9_-]+',record['id']):raise ValueError('invalid scenario identifier')
                    skip=bool(case_filter and record['id']!=case_filter)
                    if skip:continue
                    current={**record,'passed':0,'failed':0,'restarts':0};report['cases'].append(current)
                    directory=output/record['id'];directory.mkdir()
                    log=(directory/'stderr.log').open('w')
                    data=directory/'data' if data_root is None else Path(data_root)/record['id']
                    data.mkdir(parents=True)
                    process=Process(command,data.resolve(),log,protocol,cwd)
                elif skip:continue
                elif kind=='sql':
                    report['sql_attempted']+=1;actual=None
                    try:
                        actual=process.execute(record['sql']);report['sql_completed']+=1
                        check(actual,record['expected']);report['sql_passed']+=1;current['passed']+=1
                    except (AssertionError,ValueError) as error:failure(record,error,actual)
                    if report['sql_attempted']%1000==0:
                        print(f'{protocol}: {report["sql_completed"]} SQL complete, {report["sql_failed"]} failed',flush=True);save()
                elif kind=='restart':
                    process.restart(record.get('kill',False));current['restarts']+=1;report['restarts']+=1
                elif kind=='mode':
                    process.set_mode(record['mode']);current['mode_switches']=current.get('mode_switches',0)+1
                elif kind=='end':
                    process.stop();process=None;log.close();log=None
                    current['status']='PASS' if not current['failed'] else 'FAIL'
                    if not current['failed']:
                        shutil.rmtree(data)
                        shutil.rmtree(output/current['id'])
                    save()
        if case_filter and not report['cases']:raise ValueError('unknown scenario')
        report['status']='FAIL' if report['sql_failed'] else 'PASS'
    except BaseException as error:
        report['status']='FAIL';report['fatal_error']=repr(error)
        if current is not None:current['status']='FAIL'
    finally:
        if process is not None:process.stop(kill=True)
        if log is not None:log.close()
        save()
    return report
