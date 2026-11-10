import json
import os
import subprocess
import sys
import tempfile
import unittest

class ProcessRecovery(unittest.TestCase):
    def test_kill_preserves_confirmed_commit_only(self):
        with tempfile.TemporaryDirectory() as root:
            p=subprocess.Popen([sys.executable,'-m','minivectordb','--data',root],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual(json.loads(p.stdout.readline()),{'type':'ready'})
                def command(sql):
                    p.stdin.write(json.dumps({'id':'confirmed','op':'execute','sql':sql,'search_mode':'exact'})+'\n');p.stdin.flush()
                    reply=json.loads(p.stdout.readline())
                    self.assertTrue(reply['ok'],reply)
                command('CREATE TABLE t(id BIGINT PRIMARY KEY)')
                command('INSERT INTO t VALUES(1)')
                command('BEGIN')
                command('INSERT INTO t VALUES(2)')
                p.kill();p.wait(timeout=5)
            finally:
                if p.poll() is None: p.kill();p.wait()
                p.stdin.close();p.stdout.close()
            result=subprocess.run([sys.executable,'-m','minivectordb','--data',root],input=json.dumps({'id':1,'op':'execute','sql':'SELECT id FROM t;','search_mode':'exact'})+'\n'+json.dumps({'id':2,'op':'shutdown'})+'\n',text=True,capture_output=True,check=True)
            rows=json.loads(result.stdout.splitlines()[1])['rows']
            self.assertEqual(rows,[[1]])

if __name__=='__main__':unittest.main()
