#!/usr/bin/env python3
"""Commit acknowledgement, interrupted replay/checkpoint and migration boundaries.

The production binary/image is unchanged. Faults use a separately built preload
library; all markers, bind mounts and chmod calls belong to fresh test fixtures.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'protocol_check'))
from _transport import Session, ProtocolError


class Fixture(Session):
    def __init__(self,args,label):
        config=json.loads((ROOT/'protocol_check/config.json').read_text())
        config.update(cpus=args.cpus,memory=args.memory,case_timeout=600)
        super().__init__(config,args.output/(label+'.stderr'),command=[])
        self.args=args;self.root=Path(self.temp.name);os.chmod(self.root,0o777)
        if args.image:
            self.command=['docker','run','--name',self.name,'-i','--network=none',
                '--cpus='+args.cpus,'--memory='+args.memory,'--memory-swap='+args.memory,
                '--pids-limit=128','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                '--mount',f'type=bind,src={self.root},dst=/data',
                '--mount',f'type=bind,src={args.fault_library.resolve()},dst=/fault.so,readonly',
                '-e','LD_PRELOAD=/fault.so','-e','MV_NATIVE_FAULT_ROOT=/data',args.image]
        else:
            self.command=['env','LD_PRELOAD='+str(args.fault_library.resolve()),
                'MV_NATIVE_FAULT_ROOT='+str(self.root),'MINIDB_SCALAR_SUFFIX=1',args.binary,str(self.root)]

    def stop(self):
        if self.args.image and self.proc is not None:
            subprocess.run(['docker','rm','-f',self.name],capture_output=True,timeout=30)
        super().stop()

    def kill_at_marker(self,point):
        deadline=time.monotonic()+60
        marker=self.root/'fault.reached'
        while time.monotonic()<deadline:
            if marker.exists() and marker.read_text()==point:break
            if self.proc.poll() is not None:raise AssertionError(('exited before fault',point,self.proc.returncode))
            time.sleep(.01)
        else:raise AssertionError(('fault not reached',point))
        # No success response is permitted while held before returning from commit.
        assert not self.buffer and not self.selector.select(0), ('early response',point)
        if self.args.image:
            subprocess.run(['docker','kill','--signal=KILL',self.name],check=True,capture_output=True,timeout=30)
            self.proc.wait(timeout=30)
            state=json.loads(subprocess.check_output(['docker','inspect',self.name]))[0]['State']
            assert state['ExitCode']==137 and not state.get('OOMKilled'),state
        else:
            os.killpg(self.proc.pid,signal.SIGKILL);self.proc.wait(timeout=30)
            assert self.proc.returncode==-signal.SIGKILL
        self.stop();self.disarm()

    def arm(self,point):
        (self.root/'fault.arm').write_text(point)
        os.chmod(self.root/'fault.arm',0o644)

    def disarm(self):
        for name in ('fault.arm','fault.reached'):(self.root/name).unlink(missing_ok=True)


def q(s,sql):
    r=s.request(sql);assert r['ok'],(sql[:100],r);return r


def rows(s):return q(s,'SELECT id,c,v FROM t ORDER BY id')['rows']


def replace_fixture_file(s,path,data):
    # Container root has no DAC override; host cannot truncate root-owned 0644
    # files either. Replace only a file in this fresh 0777 fixture, then allow
    # the next restricted container to reopen it read/write.
    assert path.parent==s.root
    path.unlink();path.write_bytes(data);os.chmod(path,0o666)


def setup(s):
    s.start();q(s,'CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,body VARCHAR(1024),v VECTOR(2))')
    q(s,"INSERT INTO t VALUES (1,1,'"+'x'*512+"','[3,4]')")


def frame_list(path):
    data=path.read_bytes();out=[];at=0
    while at<len(data):
        n,m=struct.unpack_from('<QI',data,at);out.append((at,n,json.loads(data[at+12:at+12+m])));at+=n+24
    assert at==len(data)
    return out


def recovered(s,allowed):
    got=None
    for _ in range(2):
        s.start();actual=rows(s);assert actual in allowed,(actual,allowed)
        assert q(s,'SELECT body FROM t ORDER BY id')['rows']==[['x'*512] for _ in actual]
        if got is not None:assert actual==got
        got=actual;s.shutdown()
    doc=frame_list(s.root/'commit.log')[-1][2]
    assert {p.name for p in s.root.glob('mv-*')}==set(doc['sizes']), 'uncollected files'
    return got


def fault_case(args,point,variant):
    s=Fixture(args,point+'-'+variant)
    before=[[1,1,[3.,4.]]];ids=range(2,66) if variant=='encoded' else [2]
    after=before+[[i,2,[5.,6.]] for i in ids]
    insert='INSERT INTO t VALUES '+','.join(f"({i},2,'"+'x'*512+"','[5,6]')" for i in ids)
    try:
        setup(s)
        if variant!='auto':
            q(s,'BEGIN');q(s,insert)
            bad=s.request("INSERT INTO t VALUES (1,9,'dup','[9,9]')");assert not bad['ok']
            assert rows(s)==after
            if variant=='multi':
                q(s,"UPDATE t SET c=7 WHERE id=1");after=[[1,7,[3.,4.]]]+after[1:]
        s.arm(point)
        sql=insert if variant=='auto' else 'COMMIT'
        s.request(sql,wait=False)
        if point=='sync-eio':
            try:s.read(30)
            except ProtocolError:pass
            else:raise AssertionError('sync EIO returned a response')
            s.proc.wait(timeout=30);assert s.proc.returncode==1
            if args.image:
                state=json.loads(subprocess.check_output(['docker','inspect',s.name]))[0]['State']
                assert state['ExitCode']==1 and not state.get('OOMKilled'),state
            assert 'commit sync: Input/output error' in (args.output/(point+'-'+variant+'.stderr')).read_text()
            s.stop();s.disarm()
        else:s.kill_at_marker(point)
        allowed=[after] if point=='sync-after' else [before,after]
        result=recovered(s,allowed)
        if variant=='encoded' and point=='sync-after':
            doc=frame_list(s.root/'commit.log')[-1][2]
            assert any(x.get('encoding')=='scalar-suffix-v1' for x in doc['recipes'])
            for path in s.root.glob('mv-*'):path.unlink()
            assert recovered(s,[after])==after
        if variant=='multi' and point=='sync-after':
            assert not frame_list(s.root/'commit.log')[-1][2]['recipes']
        return {'point':point,'variant':variant,'state':result,'two_restarts':True,'ack_not_sent':True}
    finally:s.close()


def materialization(args):
    s=Fixture(args,'materialization')
    try:
        setup(s);expected=rows(s);s.shutdown()
        damages=['missing-files','truncated-vector']
        if args.materialization_policy=='checksum':damages.insert(0,'same-size-body')
        for damage in damages:
            if damage=='missing-files':
                for p in s.root.glob('mv-*'):p.unlink()
            else:
                path=next(s.root.glob('mv-*.index'));data=bytearray(path.read_bytes())
                if damage=='same-size-body':data[16:24]=struct.pack('<ff',0.,0.)
                else:data=data[:9]
                replace_fixture_file(s,path,data)
            assert recovered(s,[expected])==expected
        # Kill a replay after it starts writing a materialization, then replay again.
        for p in s.root.glob('mv-*'):p.unlink()
        s.arm('replay-write');s.start(ready=False);s.kill_at_marker('replay-write')
        assert recovered(s,[expected])==expected
        # An incomplete tail is ignored and physically truncated; old commits survive.
        size=(s.root/'commit.log').stat().st_size
        path=s.root/'commit.log'
        replace_fixture_file(s,path,path.read_bytes()+struct.pack('<QI',1000,500)+b'partial')
        recovered(s,[expected]);assert (s.root/'commit.log').stat().st_size==size
        return {'damage_cases':damages,'replay_kill':True,'partial_tail':True,
                'excluded_guarantees':[] if args.materialization_policy=='checksum' else ['same-size-body-corruption','machine-power-loss']}
    finally:s.close()


def checkpoint(args,point):
    s=Fixture(args,point);text='x'*32768
    try:
        setup(s);q(s,'CREATE TABLE wide(id BIGINT PRIMARY KEY,body VARCHAR(32768))')
        for start in range(0,280,8):
            q(s,'INSERT INTO wide VALUES '+','.join(f"({i},'{text}')" for i in range(start,start+8)))
        assert (s.root/'commit.log').stat().st_size>8<<20
        s.arm(point);s.request('DELETE FROM wide',wait=False);s.kill_at_marker(point)
        # The deletion frame is synced before checkpoint starts: it must survive.
        for _ in range(2):
            s.start();assert q(s,'SELECT id FROM wide')['rows']==[];assert rows(s)==[[1,1,[3.,4.]]];s.shutdown()
        return {'point':point,'synced_commit_survives_checkpoint':True}
    finally:s.close()


def legacy_boundary(args):
    s=Fixture(args,'legacy-boundary')
    try:
        # An actual e5 directory produced by the retained Python implementation.
        old=Session(s.config,args.output/'legacy-e5.stderr',command=[sys.executable,'-B','-m','minidb.server',str(s.root)])
        try:
            old.start();q(old,'CREATE TABLE old(id BIGINT PRIMARY KEY)');q(old,'INSERT INTO old VALUES (7)');old.shutdown()
        finally:old.close()
        before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in s.root.iterdir() if p.is_file()}
        try:s.start()
        except ProtocolError:pass
        else:raise AssertionError('legacy directory unexpectedly accepted')
        s.proc.wait(timeout=30);assert s.proc.returncode!=0;s.stop()
        after={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in s.root.iterdir() if p.is_file()}
        assert before==after,'legacy data modified'
        return {'legacy_refused_without_modification':True,'automatic_migration':False}
    finally:s.close()


def main():
    p=argparse.ArgumentParser();g=p.add_mutually_exclusive_group(required=True);g.add_argument('--image');g.add_argument('--binary')
    p.add_argument('--fault-library',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--materialization-policy',choices=['size-header','checksum'],default='size-header',
                   help='Current process-crash policy; checksum also tests body corruption on historical CRC builds.')
    p.add_argument('--cpus',default='1');p.add_argument('--memory',default='2g');args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','host_only':bool(args.binary),'target':args.image or args.binary,
            'materialization_policy':args.materialization_policy,'cases':[]}
    try:
        for variant in ('auto','single','multi','encoded'):
            for point in ('write-1','write-2','write-3','write-4','sync-before','sync-after','sync-eio'):
                report['active']=point+'-'+variant;result=fault_case(args,point,variant);report['cases'].append(result);print(report['active'],'PASS',flush=True)
        report['active']='materialization';report['cases'].append(materialization(args));print('materialization PASS',flush=True)
        for point in ('checkpoint-sync','checkpoint-before-rename','checkpoint-after-rename','checkpoint-directory'):
            report['active']=point;report['cases'].append(checkpoint(args,point));print(point,'PASS',flush=True)
        report['active']='legacy-boundary';report['cases'].append(legacy_boundary(args));report['status']='PASS'
    except BaseException as e:report['status']='FAIL';report['error']=repr(e);report['traceback']=traceback.format_exc();raise
    finally:(args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
