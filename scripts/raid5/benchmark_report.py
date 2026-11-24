#!/usr/bin/env python3
"""Paired-run report; merge histogram counts and describe repeat dispersion."""
import argparse,json,math,statistics,hashlib
from pathlib import Path
from kernel_health import alarm_lines
from benchmark_protocol import VERSION, validate
parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args();root=args.directory
m=json.loads((root/'manifest.json').read_text());groups={};observed=set()
if m.get('resume_provenance'):
 from benchmark_resume import verify_imports
 verify_imports(m['resume_provenance'])
assert len(m['results'])==len(set(m['results'])),'duplicate result paths do not count as independent repeats'
expected_keys={(i,t,r) for i in m.get('selected_cells',list(range(len(m['cells'])))) for t in ['rublk','md'] for r in range(m['repeats'])}
for name in m['results']:
 row=json.loads((root/name/'metrics.json').read_text());key=(row['cell'],row['target'],row['repeat'])
 assert key in expected_keys and key not in observed,'unexpected or duplicate cell/target/repeat evidence'
 observed.add(key)
 assert row.get('configuration',m['cells'][row['cell']])==m['cells'][row['cell']],'cell configuration differs from manifest'
 if m.get('protocol')==VERSION:
  assert row['protocol']==VERSION
  rr=row['job_ranges'];assert len(rr)==row['configuration']['jobs']
  assert all(a['offset']+a['bytes']<=b['offset'] for a,b in zip(rr,rr[1:]))
  phases=row['timing']['phases'];assert [x['phase'] for x in phases]==['warmup','measurement'] and all(x['complete'] for x in phases)
  validate(row['fio'],len(rr),m['measurement_seconds'])
 groups.setdefault((row['cell'],row['target']),[]).append(row)
def distribution(values):
 return {'median':statistics.median(values),'min':min(values),'max':max(values),'samples':values}
def percentile(hist,p):
 rank=math.ceil(sum(hist.values())*p/100);count=0
 for bound,n in sorted(hist.items()):
  count+=n
  if count>=rank:return bound
 return None
rows=[]
for index,cell in enumerate(m['cells']):
 result={'cell':index,'configuration':cell,'targets':{}}
 for target in ['rublk','md']:
  samples=groups.get((index,target),[])
  if not samples:continue
  rates=[];bandwidth=[];busy=[];steal=[];traffic=[];directions={};repeat_latencies={}
  for sample in samples:
   jobs=sample['fio']['jobs'];rates.append(sum(j[d]['iops'] for j in jobs for d in ['read','write']));bandwidth.append(sum(j[d].get('bw_bytes',j[d]['bw']*1024) for j in jobs for d in ['read','write']))
   delta=[b-a for a,b in zip(sample['before']['cpu_ticks'],sample['after']['cpu_ticks'])];total=sum(delta[:8]);busy.append(100*(total-delta[3]-delta[4]-delta[7])/total);steal.append(100*delta[7]/total)
   a=sample['before'];b=sample['after']
   if 'logical_device' in a and 'logical_device' in b:
    assert a['logical_device']['name']==b['logical_device']['name']
    logical=[y-x for x,y in zip(a['logical_device']['stat'],b['logical_device']['stat'])]
    physical={name:[y-x for x,y in zip(values,b['members'][name])] for name,values in a['members'].items()}
    written=sum(v[6] for v in physical.values())*512
    elapsed=(b['monotonic_ns']-a['monotonic_ns'])/1e9 if 'monotonic_ns' in a else None
    member_metrics={name:{'read_ios':v[0],'write_ios':v[4],'read_bytes':v[2]*512,'write_bytes':v[6]*512,'read_iops':v[0]/elapsed if elapsed else None,'write_iops':v[4]/elapsed if elapsed else None,'read_bandwidth_bytes_per_sec':v[2]*512/elapsed if elapsed else None,'write_bandwidth_bytes_per_sec':v[6]*512/elapsed if elapsed else None,'read_average_completion_ms':v[3]/v[0] if v[0] else None,'write_average_completion_ms':v[7]/v[4] if v[4] else None} for name,v in physical.items()}
    traffic.append({'members':member_metrics,'elapsed_seconds':elapsed,'counter_scope':'whole cell including warmup; block-layer request completion averages, not latency percentiles','logical_write_bytes':logical[6]*512,'logical_read_bytes':logical[2]*512,'member_write_bytes':written,'member_read_bytes':sum(v[2] for v in physical.values())*512,'member_write_over_logical_write':written/(logical[6]*512) if logical[6] else None})
   for direction in ['read','write']:
    hist=directions.setdefault(direction,{});repeat_hist={}
    for job in jobs:
     clat=job[direction]['clat_ns'];bins=clat.get('bins',{})
     assert all(isinstance(n,int) and n>=0 for n in bins.values()),'invalid histogram counts'
     if 'N' in clat:assert sum(bins.values())==clat['N'],'histogram count does not match latency sample count'
     for bucket,count in bins.items():
      midpoint=int(bucket);shift=max(0,midpoint.bit_length()-7);upper=midpoint+(1<<(shift-1))-1 if shift else midpoint;hist[upper]=hist.get(upper,0)+count;repeat_hist[upper]=repeat_hist.get(upper,0)+count
    repeat_latencies.setdefault(direction,[]).append({'repeat':sample['repeat'],'operations':sum(repeat_hist.values()),'p50_upper_ns':percentile(repeat_hist,50),'p99_upper_ns':percentile(repeat_hist,99)})
  result['targets'][target]={'whole_cell_traffic':traffic,'traffic_scope':'matched logical/member counters across full cell INCLUDING warmup; not measurement-only amplification','host_busy_scope':'full cell INCLUDING warmup, not CPU per measured fio operation','completed_repeats':len(samples),'iops':distribution(rates),'bandwidth_bytes_per_sec':distribution(bandwidth),'host_busy_percent':distribution(busy),'vm_steal_percent':distribution(steal),'cpu_scope':'guest VM /proc/stat over all vCPUs including fio+target+kernel; excludes idle/iowait/steal, not physical host CPU','completion_latency':{d:{'operations':sum(h.values()),'pooled_p50_upper_ns':percentile(h,50),'pooled_p95_upper_ns':percentile(h,95),'pooled_p99_upper_ns':percentile(h,99),'pooled_p999_upper_ns':percentile(h,99.9),'per_repeat':repeat_latencies[d],'repeat_p99_upper_ns':distribution([row['p99_upper_ns'] for row in repeat_latencies[d] if row['operations']]) if any(row['operations'] for row in repeat_latencies[d]) else None} for d,h in directions.items()}}
 if len(result['targets'])==2:
  a=result['targets']['rublk']['iops']['median'];b=result['targets']['md']['iops']['median'];result['median_iops_ratio_rublk_over_md']=a/b if b else None
 rows.append(result)
expected=len(m.get('selected_cells',m['cells']))*2*m['repeats'];collection_complete=m.get('complete',False) and observed==expected_keys
kernel_logs={name:root/('kernel-'+name+'.log') for name in ['before','after']}
kernel_alarms=sorted({line for p in kernel_logs.values() if p.exists() for line in alarm_lines(p.read_text())})
exclusion_path=root/'PERFORMANCE_INELIGIBLE.json'
exclusion=json.loads(exclusion_path.read_text()) if exclusion_path.exists() else None
kernel_evidence_complete=all(p.exists() for p in kernel_logs.values())
complete=collection_complete and kernel_evidence_complete and not kernel_alarms and exclusion is None
kernel_health={'evidence_complete':kernel_evidence_complete,'alarm_lines':kernel_alarms,'log_sha256':{name:hashlib.sha256(p.read_bytes()).hexdigest() for name,p in kernel_logs.items() if p.exists()}}

report={'resume_provenance':m.get('resume_provenance'),'validation_revision':m.get('validation_revision'),'analysis_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'scope':m['scope'],'pilot':m['pilot'],'complete':complete,'data_collection_complete':collection_complete,'performance_eligible':complete and not m['pilot'] and not m.get('protocol_check',False),'environment_exclusion':exclusion,'kernel_health':kernel_health,'completed_cells':len(m['results']),'expected_cells':expected,'binary_sha256':m['binary_sha256'],'method':'IOPS/bandwidth median and range across repeats; latency pools exact fio histogram counts; per-repeat p99 median/range separately reported, never arithmetic mean p99; guest busy excludes idle/iowait/steal and guest double counting','limits':['QEMU emulation only; no physical NVMe acceptance','instrumentation and coverage runs excluded','recovery reports separate phase-qualified windows','incomplete/pilot runs cannot satisfy full matrix acceptance'],'cells':rows}
(root/'comparison.json').write_text(json.dumps(report,indent=2)+'\n')
lines=['# Virtualized RAID5 comparison','',f"Scope: {m['scope']}. Pilot: {m['pilot']}. Complete: {complete} ({len(m['results'])}/{expected} cells).",'',f"Performance eligible: {complete and not m['pilot'] and not m.get('protocol_check',False)}. Kernel logs complete: {kernel_evidence_complete}. Kernel alarms: {len(kernel_alarms)}. Explicit exclusion: {exclusion is not None}.",'','Latency pools histogram counts; no averaged p99. IOPS is median across repeats. Physical NVMe acceptance remains deferred.','','| Cell | Workload | Block | Depth/jobs | Locality | rublk IOPS | md IOPS | Ratio |','|---|---|---|---|---|---|---|---|']
for r in rows:
 c=r['configuration'];t=r['targets'];fmt=lambda name:f"{t[name]['iops']['median']:.1f}" if name in t else 'pending';ratio=r.get('median_iops_ratio_rublk_over_md');lines.append(f"| {r['cell']} | {c['rw']} | {c['bs']} | {c['depth']}/{c['jobs']} | {c['locality']} | {fmt('rublk')} | {fmt('md')} | {ratio:.3f} |" if ratio is not None else f"| {r['cell']} | {c['rw']} | {c['bs']} | {c['depth']}/{c['jobs']} | {c['locality']} | {fmt('rublk')} | {fmt('md')} | pending |")
(root/'comparison.md').write_text('\n'.join(lines)+'\n');print('REPORT',complete,len(m['results']),expected)
