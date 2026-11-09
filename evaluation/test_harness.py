import copy
import tempfile
from pathlib import Path
import unittest
from evaluation.driver import Client
from evaluation.scoring import score

class HarnessTests(unittest.TestCase):
    def test_missing_response_has_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            c=Client(Path(directory)/'data',Path(directory)/'logs',timeout=1)
            try:
                c.process.stdin.write('SELECT ');c.process.stdin.flush()
                with self.assertRaises(TimeoutError):c.read()
            finally:c.close(kill=True)
    def test_unexpected_sql_error_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            c=Client(Path(directory)/'data',Path(directory)/'logs')
            try:
                with self.assertRaises(AssertionError):c.execute('SELECT * FROM missing')
                self.assertEqual(c.execute('SELECT * FROM missing',error=True),[])
            finally:c.close()
    def example(self):
        track={'seconds':1,'resources':{'io_bytes':1048576,'memory_peak_bytes':1048576}}
        query=dict(track,recall_at_10={'filtered':1.,'unfiltered':1.})
        return {'status':'PASS','configuration':{'rows':10000,'queries':1000,'memory':'128m','docker':True},
                'benchmark':[{'write':copy.deepcopy(track),'query':copy.deepcopy(query),'status':'PASS'} for _ in range(3)]}
    def test_formula_equal_baseline(self):
        baseline=self.example();self.assertEqual(score(baseline,baseline)['ranking_E'],'0.500000')
    def test_missing_metrics_and_invalid_recall_rejected(self):
        baseline=self.example();candidate=copy.deepcopy(baseline);candidate['benchmark'][0]['query']['resources']=None
        with self.assertRaises(ValueError):score(baseline,candidate)
        candidate=copy.deepcopy(baseline);candidate['benchmark'][0]['query']['recall_at_10']['filtered']=.94
        with self.assertRaises(ValueError):score(baseline,candidate)
    def test_slow_candidate_lowers_efficiency(self):
        baseline=self.example();candidate=copy.deepcopy(baseline)
        for run in candidate['benchmark']:
            run['write']['seconds']=2;run['query']['seconds']=2
        self.assertLess(score(baseline,candidate)['median_E'],.5)
if __name__=='__main__':unittest.main()
