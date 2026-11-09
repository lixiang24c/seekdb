import io
import json
import unittest
from minivectordb.server import write_result


class CountingOutput(io.StringIO):
    def __init__(self):
        super().__init__();self.writes=0;self.flushes=0
    def write(self,text):
        self.writes+=1
        return super().write(text)
    def flush(self):
        self.flushes+=1


class OutputBatchingTests(unittest.TestCase):
    def test_topk_reply_is_one_write_with_unchanged_frames(self):
        output=CountingOutput()
        write_result({'columns':['id','距离'],'rows':([i,i/10] for i in range(10))},output)
        frames=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(frames,[{'columns':['id','距离']}]+[{'row':[i,i/10]} for i in range(10)]+[{'ok':True}])
        self.assertEqual((output.writes,output.flushes),(1,1))

    def test_large_results_stream_before_consuming_generator(self):
        output=CountingOutput()
        def rows():
            for i in range(100):
                if i==50:self.assertGreater(output.writes,0)
                yield [i,'界'*16]
        write_result({'columns':['id','name'],'rows':rows()},output,chunk_chars=128)
        frames=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(frames),102)
        self.assertEqual(frames[-2],{'row':[99,'界'*16]})
        self.assertEqual(frames[-1],{'ok':True})
        self.assertEqual(output.writes,output.flushes)

    def test_empty_result_and_write_status_are_flushed(self):
        output=CountingOutput()
        write_result({'columns':['id'],'rows':iter(())},output)
        write_result({'affected':3},output)
        write_result(None,output)
        self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()],
                         [{'columns':['id']},{'ok':True},{'ok':True,'result':{'affected':3}},
                          {'ok':True,'result':None}])
        self.assertEqual(output.flushes,3)

    def test_failed_row_never_emits_success(self):
        output=CountingOutput()
        with self.assertRaises(ValueError):
            write_result({'columns':['v'],'rows':[[float('nan')]]},output)
        self.assertNotIn('"ok":true',output.getvalue())

if __name__=='__main__':unittest.main()
