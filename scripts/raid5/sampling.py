"""Conservative phase/frontier cohorts; histogram counts are never time-weighted."""
import math

def quantile(hist,percent):
 assert 0<percent<=100
 total=sum(hist.values())
 if not total:return None
 threshold=math.ceil(total*percent/100);count=0
 for upper,n in sorted(hist.items()):
  assert n>=0;count+=n
  if count>=threshold:return upper
 raise AssertionError('invalid histogram')

def merge(histograms):
 result={}
 for hist in histograms:
  for bucket,count in hist.items():result[bucket]=result.get(bucket,0)+count
 return result

def classify(start,end,max_latency,events,band=None):
 """Times ns; frontier logical bytes or None. Events conservatively bracket
 transitions. Histograms only permit whole-bin exclusion, never proportional
 splitting. Requiring even the longest IO to start inside the same phase
 excludes unresolved boundary cohorts. band=(start,end,'behind'|'ahead')."""
 assert start<end and max_latency>=0
 lower=start-max_latency
 spans=[(a,b) for a,b in zip(events,events[1:]) if a['t']<end and b['t']>lower]
 if not spans or spans[0][0]['t']>lower or spans[-1][1]['t']<end:return 'BOUNDARY'
 phases=set()
 for a,b in spans:
  if a['phase']!=b['phase']:return 'BOUNDARY'
  phases.add(a['phase'])
  if band:
   lo,hi,kind=band;f0=a.get('frontier');f1=b.get('frontier')
   if f0 is None or f1 is None:return 'SPATIAL_UNKNOWN'
   if kind=='behind' and hi>min(f0,f1):return 'BAND_CROSSING'
   if kind=='ahead' and lo<max(f0,f1):return 'BAND_CROSSING'
 if len(phases)!=1:return 'BOUNDARY'
 return phases.pop().upper()

def bucket_uppers(count):
 # fio 3.x groups of 64; same integer ranges as installed upstream
 # tools/hist/fio-histo-log-pctiles.py time_ranges(), in ns, not midpoint.
 assert count%64==0
 width=1;base=0;result=[]
 for group in range(count//64):
  for _ in range(64):base+=width;result.append(base)
  if group!=0:width*=2
 return result


def pure_windows(bins,duration=10_000_000_000,cohort='ACTIVE'):
 """Intersect whole eligible-bin spans across every observed job/direction."""
 streams={}
 for b in bins:
  key=(b['file'],b['direction']);streams.setdefault(key,[])
  if b['cohort']==cohort:streams[key].append((b['start_ns'],b['end_ns']))
 if not streams:return []
 common=None
 for spans in streams.values():
  merged=[]
  for start,end in sorted(spans):
   if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
   else:merged.append((start,end))
  if common is None:common=merged;continue
  common=[(max(a,c),min(b,d)) for a,b in common for c,d in merged if max(a,c)<min(b,d)]
 result=[]
 for start,end in common:
  aligned=(start+999_999_999)//1_000_000_000*1_000_000_000
  while aligned+duration<=end:result.append((aligned,aligned+duration));aligned+=duration
 return result
