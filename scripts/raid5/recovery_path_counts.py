#!/usr/bin/env python3
"""Separate instrumented memory-class path counts; never a throughput baseline."""
import argparse,collections,hashlib,json,os,re,shlex,subprocess,time
from pathlib import Path
from sampling import classify
root=Path(__file__).resolve().parents[2];os.chdir(root);assert os.geteuid()==0
p=argparse.ArgumentParser();p.add_argument('--bands',default='mixed,behind,ahead');args=p.parse_args()
assert not list(Path('/dev').glob('ublkb*'))
binary=json.loads((root/'docs/raid5/binaries/current.json').read_text());assert hashlib.sha256(Path(binary['binary']).read_bytes()).hexdigest()==binary['sha256']
out=root/'docs/raid5/recovery-path-counts'/str(time.time_ns());out.mkdir(parents=True)
manifest={'scope':'separate bpftrace-instrumented recovery diagnostic; no throughput/latency comparison acceptance','complete':False,'plain_binary':binary,'trace_sha256':hashlib.sha256((root/'scripts/raid5/recovery_paths.bt').read_bytes()).hexdigest(),'results':[]}
def save():(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
save()
for band in args.bands.split(','):
 assert band in ['mixed','behind','ahead']
 command=['/usr/bin/python3',str(root/'scripts/raid5/benchmark.py'),'--pilot','--recovery-only','--recovery-rate','128MiB','--recovery-duration','30','--recovery-band',band,'--binary',binary['binary']]
 trace=['/usr/bin/bpftrace',str(root/'scripts/raid5/recovery_paths.bt'),'-c',shlex.join(command)]
 started=time.time_ns();result=subprocess.run(trace,capture_output=True,text=True,timeout=900);text=result.stdout+result.stderr;(out/(band+'.log')).write_text(text)
 assert result.returncode==0,(band,result.returncode,text[-2500:]);assert not re.search(r'lost \d+ events',text,re.I),text[-1500:]
 names=re.findall(r'BENCHMARK_DIRECTORY (\S+)',text);assert len(names)==1,names
 directory=Path(names[0]);m=json.loads((directory/'manifest.json').read_text());assert m['complete'] and m['pilot']
 recovery=directory/'0-rublk-recovery';rows=json.loads((recovery/'timeline.json').read_text());pid=rows[-1]['status']['pid'];events=json.loads((recovery/'events.json').read_text());analysis=json.loads((recovery/'report.json').read_text())
 origin=int(re.search(r'@origin: (\d+)',text).group(1));copy=int(re.search(r'@copy: (\d+)',text).group(1));assert copy==0,copy
 frontend=set()
 for key,count in re.findall(r'@data_ring\[([^\]]+)\]: (\d+)',text):
  proc,ctx=map(lambda v:int(v.strip(),0),key.split(','))
  if proc==pid:frontend.add(ctx)
 assert len(frontend)==4,(pid,frontend)
 wall={tuple(int(v.strip(),0) for v in key.split(',')):int(value) for key,value in re.findall(r'@wall\[([^\]]+)\]: (\d+)',text)}
 counts=[];totals=collections.Counter();unknown=0
 for key,value in re.findall(r'@paths\[([^\]]+)\]: (\d+)',text):
  key=tuple(int(v.strip(),0) for v in key.split(','));bucket,proc,tid,ctx,users,fixed,retval=key
  if proc!=pid:continue
  category='rmw' if (users,fixed)==(2,1) and ctx in frontend else 'foreground_repair' if (users,fixed)==(2,0) and ctx in frontend else 'background_sync' if (users,fixed)==(2,0) and ctx not in frontend else 'other'
  if category=='other':unknown+=int(value)
  start=origin+bucket*10**9;end=start+10**9;phase=classify(start,end,wall[key],events)
  spatial=phase
  if ctx in frontend and band!='mixed':
   b=analysis['band'];spatial=classify(start,end,wall[key],events,(b['base'],b['base']+b['length'],band))
  count=int(value);totals[category]+=count;counts.append({'bucket_start_monotonic_ns':start,'bucket_end_monotonic_ns':end,'pid':proc,'tid':tid,'ring_ctx':ctx,'user_iterators':users,'kfixed_iterators':fixed,'issue_return':retval,'max_issue_wall_ns':wall[key],'path':category,'phase':phase,'spatial_phase':spatial,'bpf_invocations':count})
 assert totals['rmw']>0 and totals['background_sync']>0,(band,totals)
 if band in ['mixed','ahead']:assert totals['foreground_repair']>0,(band,totals)
 report={'band':band,'benchmark_directory':str(directory),'recovered_pid':pid,'frontend_rings':sorted(frontend),'origin_monotonic_ns':origin,'copy_callbacks':copy,'counts':counts,'totals':dict(totals),'unknown_invocations':unknown,'classification':'RMW:2USER+1KFIXED; foreground repair:2USER on ring observed in ublk_ch_uring_cmd_local; background sync:2USER on ring never observed in the data-command handler, within recovered service PID','limits':['counts are BPF invocations, not unique BIOs/client requests','tracing overhead excluded from normal performance baseline','USER/KFIXED iterator combinations crosschecked against BPF source; ordinary reads have no XOR BPF invocation','one-second groups crossing a phase/frontier or longest BPF issue interval remain excluded/unknown','no path counter is inferred for uninstrumented performance runs','md cannot supply equivalent volatile logical F; native md performance remains separate']}
 (out/(band+'-counts.json')).write_text(json.dumps(report,indent=2)+'\n');manifest['results'].append({'band':band,'started_unix_ns':started,'finished_unix_ns':time.time_ns(),'report':band+'-counts.json','totals':dict(totals),'copy':copy,'benchmark':str(directory)});save();print('PASS',band,dict(totals),flush=True)
manifest.update(complete=True,finished_unix_ns=time.time_ns(),runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest());save();print('PASS',out,flush=True)
