import ast,math,unittest
from pathlib import Path

class FlushHistogramReport(unittest.TestCase):
 def setUp(self):
  tree=ast.parse(Path(__file__).with_name('contention_report.py').read_text());context={'math':math}
  code=ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fio_sync'],type_ignores=[])
  exec(compile(code,'fio_sync','exec'),context);self.analyze=context['fio_sync']
 def test_unequal_populations_pool_counts_and_keep_actual_maximum(self):
  jobs=[{'sync':{'lat_ns':{'bins':{'1':99,'100':1},'N':100,'max':100}}},{'sync':{'lat_ns':{'bins':{'10':10000},'N':10000,'max':10}}}]
  result=self.analyze(jobs)
  self.assertEqual((result['count'],result['p50_upper_ns'],result['p99_upper_ns'],result['max_ns']),(10100,10,10,100))
 def test_missing_latency_samples_are_rejected(self):
  with self.assertRaises(AssertionError):self.analyze([{'sync':{'lat_ns':{'bins':{'10':1},'N':2,'max':10}}}])
 def test_no_flush_does_not_invent_latency(self):
  r=self.analyze([{'sync':{'lat_ns':{'N':0,'bins':{},'max':0}}}]);self.assertEqual(r['count'],0);self.assertIsNone(r['p99_upper_ns']);self.assertIsNone(r['max_ns'])

if __name__=='__main__':unittest.main()
