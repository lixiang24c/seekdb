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
                self.assertEqual(p.stdout.readline().strip(),'LOCAL_DEBUG_READY')
                def command(obj):
                    p.stdin.write(json.dumps(obj)+'\n');p.stdin.flush()
                    reply=json.loads(p.stdout.readline())
                    self.assertTrue(reply['ok'],reply)
                command({'op':'create','table':'t','columns':{'id':['BIGINT']},'primary':'id'})
                command({'op':'insert','table':'t','rows':[{'id':1}]})
                command({'op':'begin'})
                command({'op':'insert','table':'t','rows':[{'id':2}]})
                p.kill();p.wait(timeout=5)
            finally:
                if p.poll() is None: p.kill();p.wait()
                p.stdin.close();p.stdout.close()
            result=subprocess.run([sys.executable,'-m','minivectordb','--data',root],input='{"op":"scan","table":"t"}\n{"op":"quit"}\n',text=True,capture_output=True,check=True)
            rows=[json.loads(line)['row'] for line in result.stdout.splitlines()[1:] if '"row"' in line]
            self.assertEqual(rows,[{'id':1}])

if __name__=='__main__':unittest.main()
