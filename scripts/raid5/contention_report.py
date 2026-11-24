#!/usr/bin/env python3
"""Merge optional diagnostic histogram counts, never average percentiles."""
import argparse,hashlib,json,math
from pathlib import Path
root=Path(__file__).resolve().parents[2]/'docs/raid5/contention'
parser=argparse.ArgumentParser();parser.add_argument('runs',nargs='*');parser.add_argument('--output',type=Path);args=parser.parse_args()
directories=[Path(p) if Path(p).exists() else root/p for p in args.runs] if args.runs else sorted(root.iterdir())
def bounds(bins):
 n=sum(bins)
 def q(percent):
  if not n:return None
  rank=math.ceil(n*percent/100);c=0
  for i,v in enumerate(bins):
   c+=v
   if c>=rank:return 0 if i==0 else (1<<i)-1 if i<63 else None
 return {'count':n,'p50_upper_ns':q(50),'p99_upper_ns':q(99),'max_bucket_upper_ns':next(((1<<i)-1 if i<63 else None for i in range(63,-1,-1) if bins[i]),None)}
def fio_sync(jobs):
 hist={};maximum=0
 for job in jobs:
  lat=job.get('sync',{}).get('lat_ns',{});bins=lat.get('bins',{})
  assert sum(bins.values())==lat.get('N',0)
  maximum=max(maximum,lat.get('max',0))
  for bound,count in bins.items():
   midpoint=int(bound);shift=max(0,midpoint.bit_length()-7);upper=midpoint+(1<<(shift-1))-1 if shift else midpoint;hist[upper]=hist.get(upper,0)+count
 def percentile(p):
  if not sum(hist.values()):return None
  rank=math.ceil(sum(hist.values())*p/100);c=0
  for bound,n in sorted(hist.items()):
   c+=n
   if c>=rank:return bound
 return {'count':sum(hist.values()),'p50_upper_ns':percentile(50),'p99_upper_ns':percentile(99),'max_ns':maximum or None,'scope':'fio fsync completion, pooled exact histogram counts across jobs; not arithmetic mean p99'}
rows=[]
for directory in directories:
 manifest=directory/'manifest.json'
 if not manifest.exists():continue
 m=json.loads(manifest.read_text())
 if not m.get('complete'):
  assert not args.runs,('requested diagnostic incomplete',directory)
  continue
 for cell in m['results']:
  if cell['kind']!='file-backed target':continue
  metrics=json.loads((directory/(cell['name']+'-metrics.json')).read_text());names=metrics[0]['histograms'];merged={name:[sum(r['histograms'][name][i] for r in metrics) for i in range(64)] for name in names}
  progress={r['label']:{'samples':r.get('backend_bpf_cqe_progress_elapsed_ns',[]),'truncated':r.get('progress_truncated',False)} for r in metrics if r['label'].startswith('queue-')}
  cpu_file=directory/(cell['name']+'-bpf-cpu.json')
  cpu=json.loads(cpu_file.read_text()) if cpu_file.exists() else None
  if cpu is not None:
   cpu['interpretation']='bpftool omits zero run_time_ns/run_cnt (kernel tools/bpf/bpftool/prog.c:438); fields_present in older raw diagnostics means both snapshots had nonzero runtime, not support detection'
   cpu['xor_issue_run_time_ns_delta']=sum(v['run_time_ns_delta'] for v in cpu['program_deltas'] if v['name']=='xor_issue')
   cpu['xor_issue_run_cnt_delta']=sum(v['run_cnt_delta'] for v in cpu['program_deltas'] if v['name']=='xor_issue')
  fio=json.loads((directory/(cell['name']+'-fio.json')).read_text())
  rows.append({'diagnostic_binary_sha256':m['binary_sha256'],'diagnostic_seconds':m['arguments']['seconds'],'fio_fsync':fio_sync(fio['jobs']),'backend_bpf_cqe_progress':progress,'kernel_bpf_cpu_diagnostic':cpu,'all_configured_queues_over_100_handlers':all(sum(r['histograms']['request'])>100 for r in metrics if r['label'].startswith('queue-')),'run':directory.name,**cell,'histograms':{n:bounds(b) for n,b in merged.items()},'queue_completed_handlers':{r['label']:sum(r['histograms']['request']) for r in metrics if r['label'].startswith('queue-')}})
report={'analysis_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'input_runs':[str(d) for d in directories],'scope':'instrumented short file-backed pilots; no physical-NVMe scalability acceptance','aggregation':'merged bucket counts, not averaged p99','limits':['whole-run histograms include setup and teardown','old runs without CQE progress and with CPU 0-3 did not establish four continuously busy queues','CQE progress covers backend and BPF completions, not FETCH or COMMIT','BPF CQE wall latency includes scheduling; kernel CPU statistics are separately instrumented when available','no uninstrumented-versus-instrumented overhead calibration yet'],'cells':rows}
(args.output or root/'summary.json').write_text(json.dumps(report,indent=2)+'\n');print('PASS diagnostic summaries',len(rows))
