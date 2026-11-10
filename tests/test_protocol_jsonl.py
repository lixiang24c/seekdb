import io
import json
import subprocess
import sys
import tempfile
import unittest
from minivectordb.core import Database, DBError
from minivectordb.server import jsonl_main


class JsonlProtocolTests(unittest.TestCase):
    def test_ids_literals_errors_and_shutdown_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            requests=[
                {'id':9007199254740993,'op':'execute','sql':'CREATE TABLE t(id BIGINT PRIMARY KEY,s VARCHAR(80));','search_mode':'exact'},
                {'id':'中文-id','op':'execute','sql':"INSERT INTO t VALUES(1,'第一行;\n第二行');",'search_mode':'ann'},
                {'id':None,'op':'execute','sql':'ALTER TABLE t ADD x BIGINT;','search_mode':'exact'},
                {'id':True,'op':'execute','sql':'SELECT id,s FROM t;','search_mode':'exact'},
                {'id':5,'op':'execute','sql':'BEGIN;','search_mode':'exact'},
                {'id':6,'op':'execute','sql':"INSERT INTO t VALUES(2,'未提交');",'search_mode':'exact'},
                {'id':'关闭','op':'shutdown'}]
            result=subprocess.run([sys.executable,'-m','minivectordb','--data',root],
                input=''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in requests),
                text=True,capture_output=True,check=True,timeout=15)
            lines=result.stdout.splitlines();self.assertEqual(len(lines),len(requests)+1)
            messages=[json.loads(line) for line in lines];self.assertEqual(messages.pop(0),{'type':'ready'})
            for request,response in zip(requests,messages):
                self.assertEqual(type(response['id']),type(request['id']));self.assertEqual(response['id'],request['id'])
                self.assertIs(type(response['ok']),bool)
            self.assertEqual(messages[1]['affected_rows'],1)
            self.assertFalse(messages[2]['ok']);self.assertTrue(messages[2]['error']['code'])
            self.assertEqual(messages[3]['rows'],[[1,'第一行;\n第二行']])
            self.assertEqual(messages[-1],{'id':'关闭','ok':True})
            db=Database(root);self.assertEqual(list(db.rows('t')),[{'id':1,'s':'第一行;\n第二行'}]);db.close()

    def test_lazy_query_failure_never_publishes_partial_success(self):
        class BrokenSQL:
            def execute(self,text):
                def rows():
                    yield [1]
                    raise DBError('late row failure')
                return {'columns':['id'],'rows':rows()}
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);output=io.StringIO()
            source=io.StringIO('{"id":1,"op":"execute","sql":"SELECT id FROM t"}\n{"id":2,"op":"shutdown"}\n')
            jsonl_main(db,BrokenSQL(),source,output,'exact')
            messages=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(messages),3);self.assertFalse(messages[1]['ok'])
            self.assertNotIn('rows',messages[1]);self.assertEqual(messages[2],{'id':2,'ok':True})

    def test_large_result_is_one_physical_line_and_bad_json_can_continue(self):
        with tempfile.TemporaryDirectory() as root:
            db=Database(root);db.create('t',{'id':['BIGINT'],'s':['VARCHAR',80]},'id')
            db.insert('t',({'id':i,'s':'汉'*80} for i in range(2000)));db.close()
            request={'id':4,'op':'execute','sql':'SELECT id,s FROM t ORDER BY id;','search_mode':'exact'}
            text='{"id":1,"id":2,"op":"execute"}\n{"id":1e400,"op":"shutdown"}\n'+json.dumps(request)+'\n{"id":5,"op":"shutdown"}\n'
            result=subprocess.run([sys.executable,'-m','minivectordb','--data',root],input=text,
                text=True,capture_output=True,check=True,timeout=20)
            lines=result.stdout.splitlines();self.assertEqual(len(lines),5)
            self.assertFalse(json.loads(lines[1])['ok']);self.assertFalse(json.loads(lines[2])['ok']);self.assertGreater(len(lines[3]),65536)
            response=json.loads(lines[3]);self.assertEqual(len(response['rows']),2000)
            self.assertEqual(response['rows'][-1],[1999,'汉'*80])


if __name__=='__main__':unittest.main()
