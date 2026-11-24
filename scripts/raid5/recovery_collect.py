"""Phase-qualified recovery collection; no bitmap and no averaged percentiles."""
import errno,fcntl,hashlib,json,os,signal,subprocess,time
from pathlib import Path
from sampling import bucket_uppers,classify,merge,quantile,pure_windows

def resource_snapshot(members):
 result={'monotonic_ns':time.monotonic_ns(),'cpu_ticks':list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:])),'members':{}}
 for member in members or []:
  path=Path('/sys/class/block')/Path(member).name
  result['members'][path.name]=list(map(int,(path/'stat').read_text().split()))
 return result

def collect(target,dev,binary,server,log,out,capacity,seconds=15,rate='0',band='mixed',members=None,on_worker=None,pause_seconds=0,pause_after_seconds=5):
 out.mkdir();origin_mono=time.monotonic_ns();origin_unix=time.time_ns();offset=origin_unix-origin_mono
 events=[];raw=[];status_path=Path('/run/rublk-raid5/raid5-1100.status.json')
 if target=='rublk':
  os.kill(server.pid,signal.SIGKILL);server.wait(timeout=20);log.close()
  for _ in range(1000):
   r=subprocess.run([str(binary),'list','-n','1100'],capture_output=True,text=True,timeout=5)
   if 'state QUIESCED' in r.stdout:break
   time.sleep(.01)
  else:raise AssertionError(r.stdout+r.stderr)
  # QUIESCED fences ublk delivery, but old kernel ring/backend OFDs may
  # finish destruction later. Confirm actual ownership release BEFORE START
  # USER RECOVERY; never infer it solely from waitpid or QUIESCED.
  assert members is not None,'member paths required for recovery ownership fence'
  # udev may take a fresh shared flock after old-worker teardown. A
  # successful probe is a point-in-time check, not an ownership transfer.
  # Drain pending device-event readers outside the measured I/O window;
  # keep the actual production exclusive-open/flock protection unchanged.
  settle_events=[]
  def settle_udev(phase):
   began=time.monotonic_ns()
   result=subprocess.run(['/usr/bin/udevadm','settle','--timeout=30'],capture_output=True,text=True,timeout=35)
   settle_events.append({'phase':phase,'started_monotonic_ns':began,'completed_monotonic_ns':time.monotonic_ns(),'exit':result.returncode,'output':result.stdout+result.stderr,'program_sha256':hashlib.sha256(Path('/usr/bin/udevadm').read_bytes()).hexdigest()})
   (out/'ownership-udev-settle.json').write_text(json.dumps(settle_events,indent=2)+'\n')
   assert result.returncode==0,settle_events[-1]
  settle_udev('before-ownership-probe')
  fence_started=time.monotonic_ns()
  for attempt in range(3000):
   held=[]
   try:
    for member in members:
     fd=os.open(member,os.O_RDONLY|os.O_DIRECT|os.O_EXCL);held.append(fd);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    break
   except OSError as e:
    if e.errno not in (errno.EBUSY,errno.EAGAIN,errno.EWOULDBLOCK):raise
   finally:
    for fd in held:os.close(fd)
   time.sleep(.01)
  else:raise AssertionError('old ring/backend ownership did not drain')
  settle_udev('after-ownership-probe')
  (out/'ownership-fence.json').write_text(json.dumps({'started_monotonic_ns':fence_started,'completed_monotonic_ns':time.monotonic_ns(),'attempts':attempt+1,'method':'all member O_EXCL open and nonblocking OFD flock; no metadata mutation'})+'\n')
 resources_before=resource_snapshot(members)
 span=capacity if band=='mixed' else capacity//8//131072*131072
 base=0 if band!='ahead' else capacity-span
 size=span//4//131072*131072
 cmd=['fio','--name=recovery-'+target,'--filename='+str(dev),'--rw=randrw','--rwmixread=70','--bs=4k','--ioengine=io_uring','--direct=1','--iodepth=16','--numjobs=4','--size='+str(size),'--offset='+str(base),'--offset_increment='+str(size),'--time_based=1','--runtime='+str(seconds),'--randrepeat=1','--refill_buffers=1','--cpus_allowed=0,2,4,6','--cpus_allowed_policy=split','--group_reporting=1','--output-format=json+','--output='+str(out/'fio.json'),'--write_hist_log='+str(out/'latency'),'--log_hist_msec=1000','--log_unix_epoch=1']
 (out/'command.json').write_text(json.dumps(cmd,indent=2)+'\n')
 method='process-only unclean ublk recovery, retained controller cache' if target=='rublk' else 'forced-dirty md assembly native resync; clean input data, NOT actual power loss'
 if target=='md':
  assert members is not None
  trigger=[]
  def md_command(args):
   start=time.monotonic_ns();r=subprocess.run(['/usr/sbin/mdadm',*args],capture_output=True,text=True,timeout=30)
   trigger.append({'command':args,'started_monotonic_ns':start,'completed_monotonic_ns':time.monotonic_ns(),'exit':r.returncode,'output':r.stdout+r.stderr})
   (out/'md-trigger.json').write_text(json.dumps(trigger,indent=2)+'\n');assert r.returncode==0,trigger[-1]
  md_command(['--stop',str(dev)])
  md_command(['--assemble','--readonly','--update=resync','--run',str(dev),*[str(p) for p in members]])
  sys=Path('/sys/class/block/md1100/md')
  assert (sys/'resync_start').read_text().strip()=='0'
  assert (sys/'degraded').read_text().strip()=='0'
  if rate!='0':
   value=rate
   for suffix,power in [('GiB',3),('MiB',2),('KiB',1)]:
    if value.endswith(suffix):value=int(value[:-len(suffix)])*1024**power;break
   value=int(value);(sys/'sync_speed_min').write_text('1');(sys/'sync_speed_max').write_text(str(max(1,value//1024)))
  md_command(['--readwrite',str(dev)])
 fio_log=(out/'fio.log').open('w');fio=subprocess.Popen(cmd,stdout=fio_log,stderr=fio_log)
 if target=='rublk':
  log=(out/'recovered-worker.log').open('w');server=subprocess.Popen([str(binary),'recover','--foreground','-n','1100'],stdout=log,stderr=log)
  if on_worker is not None:on_worker(server,log)
 started=time.monotonic_ns();last_resource=0;done=False;sync_was_active=False;rate_set=True;before=Path('/proc/stat').read_text();last_phase=None
 first_active=None;pause_sent=False;pause_confirmed=None;resume_sent=False;pause_commands=[]
 def set_pause(value):
  command=[str(binary),'raid5-sync','-n','1100','--pause' if value else '--resume'] if target=='rublk' else ['python3','-c','from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',str(sys/'sync_action'),'frozen' if value else 'resync']
  began=time.monotonic_ns();result=subprocess.run(command,capture_output=True,text=True,timeout=30)
  pause_commands.append({'pause':value,'command':command,'started_monotonic_ns':began,'completed_monotonic_ns':time.monotonic_ns(),'exit':result.returncode,'output':result.stdout+result.stderr})
  (out/'pause-commands.json').write_text(json.dumps(pause_commands,indent=2)+'\n');assert result.returncode==0,pause_commands[-1]
 try:
  while True:
   now=time.monotonic_ns();entry={'observed_monotonic_ns':now,'observed_unix_ns':time.time_ns()}
   phase='startup';frontier=None
   if target=='rublk':
    assert server.poll() is None,(out/'recovered-worker.log').read_text()
    try:s=json.loads(status_path.read_text())
    except (FileNotFoundError,ValueError):s=None
    entry['status']=s
    if s and s['pid']==server.pid:
     assert s['state']!='ERROR',s
     if not rate_set:
      r=subprocess.run([str(binary),'raid5-sync','-n','1100','--rate',rate],capture_output=True,text=True,timeout=15);assert r.returncode==0,(r.stdout,r.stderr);rate_set=True
     sync=s['sync'];frontier=sync['frontier']*131072
     phase='post' if s['state']=='RUNNING' else 'finalizing' if sync['finalizing'] else 'paused' if sync['paused'] else 'active' if sync['active'] else 'boundary'
     done=s['state']=='RUNNING'
   else:
    values={name:(sys/name).read_text().strip() for name in ['sync_action','sync_completed','resync_start','degraded']}
    entry['md']=values;phase='post' if values['sync_action']=='idle' else 'paused' if values['sync_action']=='frozen' else 'active'
    done=values['sync_action']=='idle' and (sync_was_active or (fio.poll() is not None and now-started>=seconds*1000000000))
    # Do not call md physical progress a certified logical frontier.
   if phase!=last_phase or now-last_resource>=1000000000:
    entry['resources']=resource_snapshot(members);last_resource=now
   sync_was_active|=phase=='active'
   if phase!=last_phase:entry['event']='phase-change';last_phase=phase
   events.append({'t':now,'phase':phase,'frontier':frontier});raw.append(entry)
   if phase=='active' and first_active is None:first_active=now
   if pause_seconds:
    if not pause_sent and phase=='active' and now-first_active>=pause_after_seconds*1000000000:
     set_pause(True);pause_sent=True
    elif pause_sent and pause_confirmed is None and phase=='paused':pause_confirmed=now
    elif pause_confirmed is not None and not resume_sent and now-pause_confirmed>=pause_seconds*1000000000:
     set_pause(False);resume_sent=True
   if fio.poll() is not None and done:break
   assert now-started<300*1000000000,'recovery/foreground exceeded bounded collection'
   time.sleep(.02)
  if pause_seconds:
   assert pause_confirmed is not None and resume_sent,'sync completed before a confirmed pause/resume; not a valid paused experiment'
   (out/'pause-summary.json').write_text(json.dumps({'requested_seconds':pause_seconds,'after_active_seconds':pause_after_seconds,'first_active_monotonic_ns':first_active,'confirmed_monotonic_ns':pause_confirmed,'commands':pause_commands,'scope':'scanner transaction drain only; foreground repair may continue; finalization is not paused'},indent=2)+'\n')
  fio.wait(timeout=10);fio_log.close();assert fio.returncode==0,(out/'fio.log').read_text()
  after=Path('/proc/stat').read_text();text=(out/'fio.json').read_text();j=json.loads(text[text.index('{'):]);(out/'fio.json').write_text(json.dumps(j,indent=2)+'\n');assert all(job['error']==0 for job in j['jobs'])
  resources_after=resource_snapshot(members)
  (out/'resources.json').write_text(json.dumps({'before':resources_before,'after':resources_after,'scope':'whole collection including startup/baseline and foreground drain; member sectors use kernel 512-byte units; not isolated background traffic'},indent=2)+'\n')
  (out/'timeline.json').write_text(json.dumps(raw,indent=2)+'\n')
  metadata={'target':target,'capacity':capacity,'rate':rate,'band':band,'origin_mono':origin_mono,'origin_unix':origin_unix}
  (out/'analysis-input.json').write_text(json.dumps(metadata,indent=2)+'\n')
  analyze(out,**metadata,before=before,after=after,raw=raw)
  return server,log
 finally:
  (out/'diagnostic-timeline.json').write_text(json.dumps(raw,indent=2)+'\n')
  if fio.poll() is None:fio.kill();fio.wait(timeout=10)
  if not fio_log.closed:fio_log.close()

def cohort_summary(cohorts):
 combined={};reports=[]
 for (file,direction,phase),item in cohorts.items():
  item=dict(item);hist=merge(item.pop('histograms'));combined.setdefault((direction,phase),[]).append(hist)
  reports.append(dict(item,file=file,direction=direction,phase=phase,iops=item['operations']*1e9/item['duration_ns'],bandwidth_bytes_per_sec=item['operations']*4096e9/item['duration_ns'],p50_upper_ns=quantile(hist,50),p99_upper_ns=quantile(hist,99)))
 aggregate=[{'direction':direction,'phase':phase,'operations':sum(merge(h).values()),'p50_upper_ns':quantile(merge(h),50),'p99_upper_ns':quantile(merge(h),99)} for (direction,phase),h in combined.items()]
 return reports,aggregate

def analyze(out,target,capacity,rate,band,origin_mono,origin_unix,before='',after='',raw=None):
 out=Path(out);raw=raw if raw is not None else json.loads((out/'timeline.json').read_text());j=json.loads((out/'fio.json').read_text());offset=origin_unix-origin_mono
 span=capacity if band=='mixed' else capacity//8//131072*131072;base=0 if band!='ahead' else capacity-span
 method='process-only unclean ublk recovery, retained controller cache' if target=='rublk' else ('forced-dirty md assembly native resync; clean input data, NOT actual power loss' if (out/'md-trigger.json').exists() else 'manual parity-repair scan surrogate; NOT a genuine unclean-array restart')
 events=[]
 if target=='rublk':
  final=raw[-1]['status'];z=final['sync'];start=z['started_unix_ns']-offset;scanned=z['scan_completed_unix_ns']-offset;completed=z['completed_unix_ns']-offset
  def phase(t):return 'startup' if t<start else 'active' if t<scanned else 'finalizing' if t<completed else 'post'
  for row in raw:
   status=row.get('status')
   if status and status['pid']==final['pid']:
    t=status['sample_unix_ns']-offset;sample_phase=phase(t)
    if sample_phase=='active' and status['sync'].get('paused'):sample_phase='paused'
    events.append({'t':t,'phase':sample_phase,'frontier':status['sync']['frontier']*131072})
  events.extend([{'t':start,'phase':'active','frontier':0},{'t':scanned,'phase':'finalizing','frontier':capacity},{'t':completed,'phase':'post','frontier':capacity},{'t':raw[-1]['observed_monotonic_ns'],'phase':'post','frontier':capacity}])
 else:
  for row in raw:
   value=row['md'];events.append({'t':row['observed_monotonic_ns'],'phase':'post' if value['sync_action']=='idle' else 'paused' if value['sync_action']=='frozen' else 'active','frontier':None})
 events=sorted({e['t']:e for e in events}.values(),key=lambda e:e['t'])
 initial=min(job['job_start'] for job in j['jobs'])*1000000-offset
 category=None if band=='mixed' else (base,base+span,band)
 cohorts={};bins=[];phase_cohorts={};phase_bins=[]
 for path in out.glob('*clat_hist*'):
  previous={0:initial,1:initial}
  for row in path.read_text().splitlines():
   fields=list(map(int,row.split(',')));end=fields[0]*1000000-offset;direction=fields[1];counts=fields[3:];upper=bucket_uppers(len(counts));hist={u:n for u,n in zip(upper,counts) if n};start=previous[direction];previous[direction]=end
   if not hist or end<=start:continue
   cohort=classify(start,end,max(hist),events,category);key=(path.name,direction,cohort)
   item=cohorts.setdefault(key,{'histograms':[],'duration_ns':0,'operations':0});item['histograms'].append(hist);item['duration_ns']+=end-start;item['operations']+=sum(hist.values())
   bins.append({'file':path.name,'direction':direction,'start_ns':start,'end_ns':end,'max_latency_upper_ns':max(hist),'cohort':cohort,'operations':sum(hist.values())})
   phase_cohort=classify(start,end,max(hist),events,None);key=(path.name,direction,phase_cohort)
   item=phase_cohorts.setdefault(key,{'histograms':[],'duration_ns':0,'operations':0});item['histograms'].append(hist);item['duration_ns']+=end-start;item['operations']+=sum(hist.values())
   phase_bins.append({'file':path.name,'direction':direction,'start_ns':start,'end_ns':end,'max_latency_upper_ns':max(hist),'cohort':phase_cohort,'operations':sum(hist.values())})

 reports,aggregate=cohort_summary(cohorts)
 phase_reports,phase_aggregate=cohort_summary(phase_cohorts)
 active=[e for e in events if e['phase']=='active'];observed=sum(b['t']-a['t'] for a,b in zip(events,events[1:]) if a['phase']==b['phase']=='active')
 source_end=None
 if target=='rublk' and raw[-1].get('status'):
  z=raw[-1]['status']['sync'];source_end={'scan_elapsed_ns':z['scan_completed_unix_ns']-z['started_unix_ns'],'finalization_elapsed_ns':z['completed_unix_ns']-z['finalization_started_unix_ns']}
 report={'analysis_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'sampling_source_sha256':hashlib.sha256(Path(__file__).with_name('sampling.py').read_bytes()).hexdigest(),'target':target,'method':method,'supplementary_throttled':rate!='0','requested_rate':rate,'rate_semantics':'rublk scanner reservation budget (3*64KiB backend bytes per stripe); excludes frontend I/O and on-demand repair; md component KiB/s converted from requested bytes/s, NOT equal total physical-traffic budgets','clock':{'origin_monotonic_ns':origin_mono,'origin_unix_ns':origin_unix,'final_unix_minus_monotonic_ns':raw[-1]['observed_unix_ns']-raw[-1]['observed_monotonic_ns'],'poll_requested_ms':20,'status_publication_ms':200 if target=='rublk' else None},'band':{'kind':band,'base':base,'length':span,'md_frontier_semantics':'unknown/cannot certify F' if target=='md' else 'contiguous volatile logical F'},'observed_active_duration_ns':observed,'scan_activity_observed':bool(active),'source_durations':source_end,'default_10s_window_available':bool(pure_windows(bins)),'pure_10s_windows':pure_windows(bins),'per_job_phase_cohorts':reports,'merged_phase_quantiles':aggregate,'phase_only_per_job_cohorts':phase_reports,'phase_only_merged_quantiles':phase_aggregate,'phase_only_pure_10s_windows':pure_windows(phase_bins),'paused_pure_10s_windows':pure_windows(bins,cohort='PAUSED'),'phase_only_paused_pure_10s_windows':pure_windows(phase_bins,cohort='PAUSED'),'phase_only_default_10s_window_available':bool(pure_windows(phase_bins)),'notes':['phase p99 merges exact bucket counts; no percentile averaging','whole histogram bins only; ambiguous edges and long boundary IO excluded','per-job rates use their exact eligible durations; no fabricated aggregate rate for misaligned job cohorts','a short native sync cannot be replaced by a throttled experiment','this QEMU pilot is not physical-NVMe or genuine md crash-recovery acceptance']}
 resource_file=out/'resources.json'
 if resource_file.exists():
  resources=json.loads(resource_file.read_text());a=resources['before'];b=resources['after'];members={}
  for name,values in a['members'].items():
   delta=[y-x for x,y in zip(values,b['members'][name])]
   members[name]={'counter_delta':delta,'read_bytes':delta[2]*512,'write_bytes':delta[6]*512}
  foreground_write=sum(job['write']['io_bytes'] for job in j['jobs'])
  report['whole_collection_resources']={'scope':resources['scope'],'elapsed_ns':b['monotonic_ns']-a['monotonic_ns'],'members':members,'foreground_write_bytes':foreground_write,'combined_member_write_over_foreground_write':sum(v['write_bytes'] for v in members.values())/foreground_write if foreground_write else None,'ratio_limit':'combined startup+sync+foreground+drain traffic; NOT pure background write amplification or phase-isolated ratio'}
 else:report['whole_collection_resources']={'available':False,'reason':'older collector did not record member counters; do not invent traffic'}
 (out/'timeline.json').write_text(json.dumps(raw,indent=2)+'\n');(out/'events.json').write_text(json.dumps(events,indent=2)+'\n');(out/'bin-eligibility.json').write_text(json.dumps(bins,indent=2)+'\n');(out/'report.json').write_text(json.dumps(report,indent=2)+'\n');(out/'phase-only-bin-eligibility.json').write_text(json.dumps(phase_bins,indent=2)+'\n')
 if before:(out/'cpu-before.txt').write_text(before)
 if after:(out/'cpu-after.txt').write_text(after)
 return report
