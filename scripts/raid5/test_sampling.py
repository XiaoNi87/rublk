import unittest
from sampling import quantile,merge,classify,bucket_uppers,pure_windows
class Sampling(unittest.TestCase):
 def events(self,phase='active'):return [{'t':i,'phase':phase,'frontier':i*10} for i in range(0,101,10)]
 def test_known_histograms_merge_counts_not_percentiles(self):
  a={1:990,100:10};b={1:1,100:99};self.assertEqual(quantile(a,99),1);self.assertEqual(quantile(b,99),100)
  self.assertEqual(quantile(merge([a,b]),99),100)
 def test_native_sync_ends_before_warmup(self):
  ev=[{'t':0,'phase':'active'},{'t':10,'phase':'active'},{'t':11,'phase':'post'},{'t':200,'phase':'post'}]
  self.assertEqual(classify(30,150,1,ev),'POST')
  self.assertEqual(classify(1,9,0,ev),'ACTIVE')
 def test_mid_bin_completion_not_fractionally_split(self):
  ev=[{'t':0,'phase':'active'},{'t':15,'phase':'active'},{'t':16,'phase':'post'},{'t':100,'phase':'post'}]
  self.assertEqual(classify(10,20,0,ev),'BOUNDARY')
 def test_unequal_completion_targets(self):
  a=self.events();b=self.events('post')
  self.assertEqual(classify(20,30,1,a),'ACTIVE');self.assertEqual(classify(20,30,1,b),'POST')
 def test_frontier_crossing_and_unknown(self):
  ev=self.events();self.assertEqual(classify(20,30,0,ev,(100,190,'behind')),'ACTIVE')
  self.assertEqual(classify(20,30,0,ev,(250,350,'ahead')),'BAND_CROSSING')
  self.assertEqual(classify(20,30,0,ev,(400,500,'ahead')),'ACTIVE')
  self.assertEqual(classify(20,30,0,[dict(e,frontier=None) for e in ev],(400,500,'ahead')),'SPATIAL_UNKNOWN')
  self.assertEqual(classify(20,30,0,[dict(e,frontier=None) for e in ev],None),'ACTIVE')
 def test_paused_finalization_distinct(self):
  self.assertEqual(classify(20,30,0,self.events('paused')),'PAUSED');self.assertEqual(classify(20,30,0,self.events('finalizing')),'FINALIZING')
 def test_long_boundary_io_excluded(self):
  ev=[{'t':0,'phase':'paused'},{'t':10,'phase':'active'},{'t':100,'phase':'active'}]
  self.assertEqual(classify(20,30,15,ev),'BOUNDARY');self.assertEqual(classify(20,30,5,ev),'ACTIVE')
 def test_short_unresolved_sync_not_steady_state(self):
  self.assertEqual(classify(0,10,0,[{'t':1,'phase':'active'},{'t':5,'phase':'post'}]),'BOUNDARY')
 def test_common_windows_cannot_bridge_pause_or_missing_job(self):
  ns=1_000_000_000
  bins=[dict(file=f,direction=0,start_ns=i*ns,end_ns=(i+1)*ns,cohort='PAUSED' if i==5 else 'ACTIVE') for f in ['a','b'] for i in range(12)]
  self.assertEqual(pure_windows(bins),[])
  for b in bins:b['cohort']='ACTIVE'
  self.assertEqual(pure_windows(bins),[(0,10*ns)])
  bins.append(dict(file='c',direction=0,start_ns=0,end_ns=ns,cohort='BOUNDARY'));self.assertEqual(pure_windows(bins),[])
 def test_fio_bucket_ranges(self):
  upper=bucket_uppers(1856);self.assertEqual(upper[:3],[1,2,3]);self.assertEqual(upper[127],128);self.assertEqual(upper[128],130);self.assertEqual(len(upper),1856)
if __name__=='__main__':unittest.main()
