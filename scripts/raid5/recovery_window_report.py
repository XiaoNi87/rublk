#!/usr/bin/env python3
"""Post-measurement ten-second window quantiles from whole fio histogram bins."""
import argparse,hashlib,json
from pathlib import Path
from sampling import bucket_uppers,quantile
parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args();out=args.directory
report=json.loads((out/'report.json').read_text());metadata=json.loads((out/'analysis-input.json').read_text());offset=metadata['origin_unix']-metadata['origin_mono']
raw={}
for path in out.glob('*clat_hist*'):
 for line in path.read_text().splitlines():
  f=list(map(int,line.split(',')));upper=bucket_uppers(len(f)-3);hist={u:n for u,n in zip(upper,f[3:]) if n}
  raw.setdefault((path.name,f[1],f[0]*1000000-offset),hist)
modes=[]
for name,windows,bins_file,cohort in [('phase-only',report['phase_only_pure_10s_windows'],'phase-only-bin-eligibility.json','ACTIVE'),('spatially-qualified',report['pure_10s_windows'],'bin-eligibility.json','ACTIVE'),('paused-phase-only',report.get('phase_only_paused_pure_10s_windows',[]),'phase-only-bin-eligibility.json','PAUSED'),('paused-spatially-qualified',report.get('paused_pure_10s_windows',[]),'bin-eligibility.json','PAUSED')]:
 bins=json.loads((out/bins_file).read_text());rows=[]
 for start,end in windows:
  streams={};combined={}
  for b in bins:
   if b['cohort']!=cohort or b['start_ns']<start or b['end_ns']>end:continue
   key=(b['file'],b['direction']);item=streams.setdefault(key,{'hist':{},'duration_ns':0,'operations':0});hist=raw[(b['file'],b['direction'],b['end_ns'])]
   assert sum(hist.values())==b['operations']
   for bound,n in hist.items():item['hist'][bound]=item['hist'].get(bound,0)+n
   item['duration_ns']+=b['end_ns']-b['start_ns'];item['operations']+=b['operations']
  per_stream=[]
  for (file,direction),item in streams.items():
   hist=item.pop('hist');pool=combined.setdefault(direction,{})
   for bound,n in hist.items():pool[bound]=pool.get(bound,0)+n
   per_stream.append(dict(item,file=file,direction=direction,iops=item['operations']*1e9/item['duration_ns'],p50_upper_ns=quantile(hist,50),p99_upper_ns=quantile(hist,99)))
  rows.append({'start_monotonic_ns':start,'end_monotonic_ns':end,'per_stream':per_stream,'pooled_quantiles':[{'direction':direction,'operations':sum(hist.values()),'p50_upper_ns':quantile(hist,50),'p99_upper_ns':quantile(hist,99)} for direction,hist in combined.items()]})
 modes.append({'mode':name,'cohort':cohort,'windows':rows,'available':bool(rows)})
summary={'analysis_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'input_report_sha256':hashlib.sha256((out/'report.json').read_bytes()).hexdigest(),'target':report['target'],'method':report['method'],'band':report['band'],'supplementary_throttled':report['supplementary_throttled'],'modes':modes,'rules':['ten-second envelopes are fully inside common corresponding ACTIVE or PAUSED coverage of all streams','only complete eligible histogram bins inside each envelope are retained; edge bins omitted, never split or weighted','each stream rate uses its actual retained-bin duration, not an invented full-envelope operation count','pooled quantiles merge exact histogram bucket counts across retained streams; never average p99','no aggregate IOPS when stream coverage differs; unavailable spatial md frontier remains unavailable','older inputs without member counters cannot establish write amplification']}
(out/'window-report.json').write_text(json.dumps(summary,indent=2)+'\n');print('WINDOW_REPORT',[(m['mode'],len(m['windows'])) for m in modes])
