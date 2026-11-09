import unittest
from tests.resource_metrics import check
class ResourceMetricTests(unittest.TestCase):
    def example(self):return {'peak':64*1024*1024,'limit':64*1024*1024,'current':63*1024*1024,'swap_limit':0,'events':{'max':200,'oom':0,'oom_kill':0}}
    def test_controller_completion_and_raw_peak_are_separate(self):
        stats=self.example();stats['peak']+=4096
        result=check(stats)
        self.assertTrue(result['controller_pass']);self.assertFalse(result['strict_peak_pass'])
        self.assertEqual(result['peak'],stats['peak']);self.assertEqual(result['transient_peak_excess_bytes'],4096)
        with self.assertRaises(AssertionError):check(stats,strict=True)
    def test_oom_and_swap_are_never_accepted(self):
        for event in ('oom','oom_kill','oom_group_kill'):
            stats=self.example();stats['events'][event]=1
            with self.assertRaises(AssertionError):check(stats)
        stats=self.example();del stats['events']['oom']
        with self.assertRaises(AssertionError):check(stats)
        stats=self.example();stats['swap_limit']=1
        with self.assertRaises(AssertionError):check(stats)
    def test_strict_peak_pass(self):self.assertTrue(check(self.example(),strict=True)['strict_peak_pass'])
if __name__=='__main__':unittest.main()
