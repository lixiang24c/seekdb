import gzip
import json
import tempfile
import unittest
from pathlib import Path
from evaluation.stress_generate import generate
from evaluation.stress_model import Model,f32
from evaluation.stress_run import check,Process


class StressHarnessTests(unittest.TestCase):
    def test_reference_distance_rounding_and_transaction_atomicity(self):
        model=Model();model.create('t',2)
        model.insert('t',[{'id':1,'tag':0,'label':'😀','n':0,'v':[3,4]}])
        expr=('distance',(0,0))
        for numpy in (False,True):
            result,ann=model.select('t',[('id',None),(expr,'d')],[],[(expr,False)],10,use_numpy=numpy)
            self.assertEqual(result['rows'],[[1,5.]])
        self.assertEqual(f32(16777217),16777216.)
        model.begin();model.modify('t',[],{'v':[0,0]});model.finish(False)
        self.assertEqual(model.tables['t'].rows[1]['v'],[3.,4.])
        with self.assertRaises(ValueError):model.insert('t',[{'id':2,'tag':0,'label':'ok','n':0,'v':[0,0]},model.tables['t'].rows[1]])
        self.assertEqual(list(model.tables['t'].rows),[1])
    def test_seed_reproduces_bytes_and_at_least_requested_sql(self):
        with tempfile.TemporaryDirectory() as root:
            a=Path(root)/'a.gz';b=Path(root)/'b.gz'
            first=generate(a,Path('mysqltest'),200,2,19)
            second=generate(b,Path('mysqltest'),200,2,19)
            self.assertEqual(a.read_bytes(),b.read_bytes())
            self.assertEqual(first['sha256'],second['sha256'])
            self.assertGreaterEqual(first['counts']['sql'],200)
            with gzip.open(a,'rt') as source:records=[json.loads(line) for line in source]
            self.assertEqual(sum(r['kind']=='sql' for r in records),first['counts']['sql'])
    def test_checker_rejects_squared_distance_and_wrong_success(self):
        with self.assertRaises(AssertionError):check({'rows':[[1,25.]]},{'rows':[[1,5.]]})
        with self.assertRaises(AssertionError):check({'rows':[]},{'error':True})
        with self.assertRaises(AssertionError):check({'rows':[[1]]},{'columns':['id'],'rows':[[1]]})
        with self.assertRaises(AssertionError):check({'rows':[[[0.1]]]},{'rows':[[[f32(0.1)]]]})
        check({'rows':[[1,5.]],'columns':['id','d']},{'rows':[[1,5.]],'columns':['id','d']})
    def test_text_adapter_accepts_both_status_forms(self):
        import sys
        code='import sys\nfor line in sys.stdin:\n print("OK" if "CREATE" in line else "OK 0",flush=True)'
        with tempfile.TemporaryDirectory() as root:
            with (Path(root)/'stderr.log').open('w') as log:
                process=Process([sys.executable,'-u','-c',code],root,log,'text')
                try:
                    self.assertEqual(process.execute('CREATE TABLE t(id BIGINT PRIMARY KEY)')['affected_rows'],0)
                    self.assertEqual(process.execute('SELECT id FROM t')['rows'],[])
                finally:process.stop()

if __name__=='__main__':unittest.main()
