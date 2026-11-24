#!/usr/bin/env python3
"""Exclusive paired NVMe/md harness. --pilot validates tooling, NOT acceptance.
Destructively recreates only the three serial-identified authorized members; never assume-clean.
"""
import argparse,signal,gzip,shutil,json,os,subprocess,time,hashlib
from pathlib import Path
from nvme_members import resolve_members
from kernel_health import KernelHealthGuard
from benchmark_protocol import VERSION, VALIDATION_REVISION, ranges, run_phases
root=Path(__file__).resolve().parents[2];os.chdir(root)
parser=argparse.ArgumentParser();parser.add_argument('--pilot',action='store_true');parser.add_argument('--pilot-queues',type=int,choices=[1,4],default=4);parser.add_argument('--recovery',action='store_true');parser.add_argument('--recovery-only',action='store_true');parser.add_argument('--recovery-rate',default='0');parser.add_argument('--recovery-duration',type=int,default=15);parser.add_argument('--recovery-band',choices=['mixed','behind','ahead'],default='mixed');parser.add_argument('--binary',default='target/release/rublk');parser.add_argument('--recovery-pause-seconds',type=int,default=0);parser.add_argument('--recovery-pause-after',type=int,default=5);parser.add_argument('--protocol-check',action='store_true',help='one repeat of original high-depth cell 52 on both targets; never full acceptance');parser.add_argument('--resume-from',type=Path);args=parser.parse_args()
if args.recovery_only:args.recovery=True
if args.recovery_duration<1:parser.error('--recovery-duration must be positive')
if args.recovery_pause_seconds<0 or args.recovery_pause_after<0:parser.error('pause durations must be nonnegative')
if args.recovery_pause_seconds and not args.recovery:parser.error('pause scenario requires --recovery or --recovery-only')
if args.recovery_pause_seconds and args.recovery_duration<args.recovery_pause_after+args.recovery_pause_seconds+10:parser.error('foreground duration must cover pause delay, pause, and at least ten additional seconds')
assert os.geteuid()==0
binary=(root/args.binary).resolve();assert binary.exists()
elf=subprocess.run(['readelf','--wide','--sections',str(binary)],capture_output=True,text=True,check=True).stdout
assert '__llvm_prf' not in elf and '__llvm_cov' not in elf,'never benchmark instrumented code'
assert json.loads((root/'docs/raid5/nvme-preparation.json').read_text())['complete']
node=Path('/dev/ublkb1100');md=Path('/dev/md1100');members=resolve_members()
assert not node.exists() and not md.exists()
capacity=4292870144
out=root/'docs/raid5/benchmarks'/(str(time.time_ns())+('-pilot' if args.pilot else '-full'));out.mkdir(parents=True)
warmup,measure,repeats=(1,5,1) if args.pilot else (30,120,1 if args.protocol_check else 5)
assert not (args.pilot and args.protocol_check), 'choose pilot or protocol-check'
base_cells=[{'rw':'randread','bs':'4k','depth':16,'jobs':4,'locality':'disjoint','fsync':0}, {'rw':'randwrite','bs':'4k','depth':16,'jobs':4,'locality':'disjoint','fsync':0}, {'rw':'write','bs':'128k','depth':16,'jobs':4,'locality':'disjoint','fsync':0}, {'rw':'randrw','mix':70,'bs':'4k','depth':16,'jobs':4,'locality':'disjoint','fsync':0}]
cells=list(base_cells)
if args.pilot:
 cells=[dict(cell,jobs=args.pilot_queues) for cell in cells]
if not args.pilot:
 # Dimension coverage, not an undisclosed Cartesian product. Manifest is exact.
 for rw in ['read','write','randread','randwrite','randrw']:
  for bs in ['4k','64k','128k','256k','1m']:
   cells.append({'rw':rw,'mix':50,'bs':bs,'depth':16,'jobs':4,'locality':'disjoint','fsync':0})
 for depth in [1,16,128]:
  for jobs in [1,4]:
   for bs in ['4k','128k']:
    for locality in ['disjoint','hotspot']:
     cells.append({'rw':'randwrite','bs':bs,'depth':depth,'jobs':jobs,'locality':locality,'fsync':0})
 for fsync in [1,32]:cells.append({'rw':'randwrite','bs':'4k','depth':16,'jobs':4,'locality':'hotspot','fsync':fsync})
if args.recovery_only:cells=[]
manifest={'mode':'recovery-only' if args.recovery_only else 'steady-with-recovery' if args.recovery else 'steady-only','pilot':args.pilot,'warmup_seconds':warmup,'measurement_seconds':measure,'repeats':repeats,'cells':cells,'queue_grouping':'one job uses one ublk queue; four jobs use four queues; exclusive recreation and independent parity per queue group','capacity':capacity,'members':list(map(str,members)),'scope':'QEMU virtual NVMe, not physical-NVMe acceptance','recovery':'separate event-window collection; steady-state results only here','results':[],'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'source_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('src/raid5').glob('*.rs')}}
from source_manifest import fingerprint
manifest['full_source_sha256']=fingerprint()
manifest['protocol']=VERSION
manifest['validation_revision']=VALIDATION_REVISION
manifest['protocol_check']=args.protocol_check
manifest['selected_cells']=[52] if args.protocol_check else list(range(len(cells)))
manifest['timing']='independent warmup and measurement processes; ramp_time=0; per-job start skew <=1000ms; completion end skew diagnostic only; runtime includes completion tail; each phase has 60s bounded shutdown allowance; excessive drain invalidates sample'
manifest['hotspot']='128KiB per-job region; all job ranges disjoint'
manifest['harness_arguments']={k:str(v) if isinstance(v,Path) else v for k,v in vars(args).items()}
manifest['host_boot_id']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
manifest['program_sha256']={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in [('fio','/usr/bin/fio'),('mdadm','/usr/sbin/mdadm'),('python3','/usr/bin/python3')]}

manifest['member_queue_configuration']={d.name:{name:((Path('/sys/class/block')/d.name/'queue'/name).read_text().strip() if (Path('/sys/class/block')/d.name/'queue'/name).exists() else 'unavailable') for name in ['logical_block_size','physical_block_size','write_cache','scheduler','nomerges','max_sectors_kb','nr_requests','rotational','dma_alignment']} for d in members}
resume_evidence=None;done=set();recovery_done=set()
if args.resume_from:
 assert not args.pilot and not args.protocol_check and not args.recovery_only, 'resume requires unchanged full matrix'
 from benchmark_resume import audit,install,pending
 resume_evidence,done,recovery_done=audit(args.resume_from,manifest)
 install(resume_evidence,out,manifest)
 manifest['continuation_plan']=pending(cells,repeats,done,recovery_done,args.recovery)
 print('RESUME_AUDITED',len(done),len(recovery_done),'PENDING',sum(len(x['cells']) for x in manifest['continuation_plan']),flush=True)
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
health=KernelHealthGuard(out)
def interrupted(signum,frame):
 raise KeyboardInterrupt('benchmark termination requested')
signal.signal(signal.SIGTERM,interrupted)

def command(name,cmd,timeout=180,allowed=(0,)):
 child=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
 try:
  stdout,stderr=child.communicate(timeout=timeout)
 except BaseException:
  try:os.killpg(child.pid,signal.SIGTERM)
  except ProcessLookupError:pass
  try:stdout,stderr=child.communicate(timeout=10)
  except subprocess.TimeoutExpired:
   try:os.killpg(child.pid,signal.SIGKILL)
   except ProcessLookupError:pass
   stdout,stderr=child.communicate(timeout=10)
  (out/(name+'.log')).write_text(stdout+stderr)
  raise
 (out/(name+'.log')).write_text(stdout+stderr)
 assert child.returncode in allowed,(name,stdout[-1000:],stderr[-3000:])
 health.check()
 return subprocess.CompletedProcess(cmd,child.returncode,stdout,stderr)

def worker_placement(target,server):
 rows=[]
 if target=='rublk':
  tasks=Path(f'/proc/{server.pid}/task');candidates=list(tasks.iterdir()) if tasks.exists() else []
 else:
  candidates=[]
  for proc in Path('/proc').iterdir():
   if not proc.name.isdigit():continue
   try:
    if (proc/'comm').read_text().strip().startswith('md1100'):candidates.append(proc)
   except OSError:pass
 for task in candidates:
  try:
   status=dict(line.split(':',1) for line in (task/'status').read_text().splitlines() if ':' in line)
   stat=(task/'stat').read_text().split(') ',1)[1].split()
   rows.append({'tid':int(task.name),'name':(task/'comm').read_text().strip(),'allowed_cpus':status.get('Cpus_allowed_list','unknown').strip(),'last_cpu_at_sample':int(stat[36]),'user_ticks':int(stat[11]),'system_ticks':int(stat[12]),'sample_monotonic_ns':time.monotonic_ns()})
  except (OSError,ValueError,IndexError):continue
 return rows

def snapshot():
 return {'logical_device':{'name':dev.name,'stat':list(map(int,(Path('/sys/class/block')/dev.name/'stat').read_text().split()))},'monotonic_ns':time.monotonic_ns(),'unix_ns':time.time_ns(),'cpu_ticks':list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:])),'members':{d.name:list(map(int,(Path('/sys/class/block')/d.name/'stat').read_text().split())) for d in members}}

def zero(repeat,target):
 assert not node.exists() and not md.exists()
 keys=set()
 for dev in members:
  sys=Path('/sys/class/block')/dev.name;assert not list((sys/'holders').iterdir());assert not list(sys.glob(dev.name+'p*'))
  keys.add((sys/'dev').read_text().strip())
 for line in Path('/proc/self/mountinfo').read_text().splitlines():assert line.split()[2] not in keys
 # Exclusive open prevents accidental overwriting of an active block holder.
 for dev in members:
  fd=os.open(dev,os.O_RDWR|os.O_EXCL);os.close(fd)
 for dev in members:
  command(f'zero-{repeat}-{target}-{dev.name}',['fio','--name=authorized-precondition-zero','--filename='+str(dev),'--rw=write','--size=2G','--bs=1M','--direct=1','--ioengine=io_uring','--iodepth=16','--zero_buffers=1','--end_fsync=1','--output-format=json'])

def start(target,repeat,queues=4):
 zero(repeat,target);begin=time.monotonic_ns();server=None;log=None
 if target=='rublk':
  log=(out/f'server-{repeat}.log').open('w');server=subprocess.Popen([str(binary),'add','raid5','-z','--foreground','-n','1100','-q',str(queues),'-d','128','-b','1MiB','--sync-rate',args.recovery_rate if args.recovery else '0',*map(str,members)],stdout=log,stderr=log)
  for _ in range(30000):
   assert server.poll() is None,(out/f'server-{repeat}.log').read_text()
   if node.exists():
    state=subprocess.run([str(binary),'list','-n','1100'],capture_output=True,text=True,timeout=5)
    if state.returncode==0 and 'state LIVE' in state.stdout:break
   time.sleep(.01)
  else:raise AssertionError('RAID5 startup timeout')
  dev=node
 else:
  command('md-create-'+str(repeat),['/usr/sbin/mdadm','--create',str(md),'--run','--metadata=1.2','--level=5','--raid-devices=3','--chunk=64','--layout=left-symmetric','--size=2096128','--data-offset=1M','--bitmap=none','--consistency-policy=resync',*map(str,members)])
  command('md-initialization-'+str(repeat),['/usr/sbin/mdadm','--wait',str(md)],timeout=300,allowed=(0,1))
  initialization=[]
  for _ in range(3000):
   sys=Path('/sys/class/block/md1100/md');sample={name:(sys/name).read_text().strip() for name in ['sync_action','sync_completed','resync_start','degraded']};sample['monotonic_ns']=time.monotonic_ns();initialization.append(sample)
   if sample['sync_action']=='idle' and sample['resync_start']=='none' and sample['degraded']=='0':break
   time.sleep(.1)
  else:raise AssertionError(initialization[-1])
  (out/f'md-initialization-timeline-{repeat}.json').write_text(json.dumps(initialization,indent=2)+'\n')
  sys=Path('/sys/class/block/md1100/md');assert (sys/'sync_action').read_text().strip()=='idle';assert (sys/'consistency_policy').read_text().strip()=='resync'
  tuning={p.name:p.read_text().strip() for p in sys.iterdir() if p.name in ['stripe_cache_size','group_thread_cnt','sync_speed_min','sync_speed_max','consistency_policy','layout','chunk_size','component_size','raid_disks','metadata_version']}
  (out/f'md-tuning-{repeat}.json').write_text(json.dumps(tuning,indent=2)+'\n');dev=md
 actual=int(command('capacity-'+target+'-'+str(repeat),['blockdev','--getsize64',str(dev)]).stdout);assert actual==capacity,actual
 manifest.setdefault('startup',[]).append({'target':target,'repeat':repeat,'elapsed_ns':time.monotonic_ns()-begin,'includes':'read-only adoption and metadata' if target=='rublk' else 'kernel initialization checkpoint and no-degraded wait; see raw timeline'})
 manifest.setdefault('worker_placement',[]).append({'target':target,'repeat':repeat,'configured_ublk_queues':queues if target=='rublk' else None,'threads':worker_placement(target,server),'scope':'startup affinity and last-CPU observation, not proof every completion stays on that CPU'})
 # Identical compressibility pattern / full data preparation, before sampling.
 command('fill-'+target+'-'+str(repeat),['fio','--name=identical-fill','--filename='+str(dev),'--rw=write','--bs=128k','--size='+str(capacity),'--direct=1','--ioengine=io_uring','--iodepth=32','--refill_buffers=1','--randrepeat=1','--end_fsync=1','--output-format=json'],timeout=300)
 command('verify-'+target+'-'+str(repeat),['fio','--name=independent-content','--filename='+str(dev),'--rw=write','--bs=4k','--size=128M','--direct=1','--ioengine=io_uring','--iodepth=32','--verify=crc32c','--do_verify=1','--verify_fatal=1','--output-format=json'])
 return dev,server,log

def stop(target,server,log,repeat,require_success=True):
 if target=='rublk':
  if node.exists():command('stop-rublk-'+str(repeat),[str(binary),'del','-n','1100'],timeout=30)
  server.wait(timeout=30);log.close()
  if require_success:assert server.returncode==0
  # Rebuild independent checker if cargo clean removed it.
  command('checker-build',['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/check_physical.c','-o',str(out/'check-physical')])
  command('rublk-physical-'+str(repeat),[str(out/'check-physical'),*map(str,members)],timeout=180)
 else:
  sys=Path('/sys/class/block/md1100/md');(sys/'sync_action').write_text('check')
  command('md-check-'+str(repeat),['/usr/sbin/mdadm','--wait',str(md)],timeout=300,allowed=(0,1))
  for _ in range(30000):
   if (sys/'sync_action').read_text().strip()=='idle':break
   time.sleep(.01)
  else:raise AssertionError('md post-workload parity check did not finish')
  assert int((sys/'mismatch_cnt').read_text())==0
  command('stop-md-'+str(repeat),['/usr/sbin/mdadm','--stop',str(md)],timeout=30)
  command('checker-build-md',['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/check_physical.c','-o',str(out/'check-physical')])
  command('md-physical-'+str(repeat),[str(out/'check-physical'),*map(str,members)],timeout=180)
  for _ in range(500):
   if not md.exists():break
   time.sleep(.01)
  if md.exists() and not Path('/sys/class/block/md1100/md').exists():md.unlink()
 assert not node.exists() and not md.exists()

for name,cmd in [('kernel',['uname','-a']),('cpu',['lscpu']),('block',['lsblk','-O','--json']),('fio-version',['fio','--version']),('mdadm-version',['/usr/sbin/mdadm','--version']),('source',['git','-c','safe.directory='+str(root),'status','--short'])]:command(name,cmd)
try:
 for repeat in range(repeats):
  for target in (['rublk','md'] if repeat%2==0 else ['md','rublk']):
   for queues in ([4] if args.recovery_only or args.protocol_check else [args.pilot_queues] if args.pilot else [4,1]):
    if args.resume_from and not any(cell['jobs']==queues and (repeat,target,i) not in done for i,cell in enumerate(cells)) and not (args.recovery and queues==4 and (repeat,target) not in recovery_done):continue
    group_repeat=f'{repeat}-q{queues}'
    dev,server,log=start(target,group_repeat,queues)
    holder={'server':server,'log':log};completed=False
    try:
     if args.recovery and queues==4 and (repeat,target) not in recovery_done:
      from recovery_collect import collect
      server,log=collect(target,dev,binary,server,log,out/f'{repeat}-{target}-recovery',capacity,seconds=args.recovery_duration,rate=args.recovery_rate,band=args.recovery_band,members=members,on_worker=lambda p,l:holder.update(server=p,log=l),pause_seconds=args.recovery_pause_seconds,pause_after_seconds=args.recovery_pause_after)
      health.check()
      manifest.setdefault('recovery_results',[]).append(f'{repeat}-{target}-recovery');(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('RECOVERY_COLLECTED',target,flush=True)
     for index,cell in enumerate(cells):
      if cell['jobs']!=queues or index not in manifest['selected_cells'] or (repeat,target,index) in done:continue
      stem=f'{repeat}-{target}-{index}';directory=out/stem;directory.mkdir();jobs=cell['jobs'];job_ranges=ranges(cell,capacity);size=job_ranges[0]['bytes']
      cmd=['fio','--name='+stem,'--filename='+str(dev),'--rw='+cell['rw'],'--rwmixread='+str(cell.get('mix',50)),'--bs='+cell['bs'],'--ioengine=io_uring','--direct=1','--iodepth='+str(cell['depth']),'--numjobs='+str(jobs),'--size='+str(size),'--offset_increment='+str(size),'--fsync='+str(cell['fsync']),'--time_based=1','--randrepeat=1','--refill_buffers=1','--cpus_allowed=0,2,4,6','--cpus_allowed_policy=split']
      assert shutil.disk_usage(out).free>128*1048576,'less than 128MiB evidence space; refusing next cell before disk exhaustion'
      (directory/'job-ranges.json').write_text(json.dumps(job_ranges,indent=2));before=snapshot();job=run_phases(cmd,directory,stem,warmup,measure,jobs,command);after=snapshot()
      for history in directory.glob('*.log'):
       with history.open('rb') as source,gzip.open(str(history)+'.gz','wb',compresslevel=6) as dest:shutil.copyfileobj(source,dest)
       history.unlink()
      report={'target':target,'repeat':repeat,'cell':index,'configuration':cell,'aggregate_depth':jobs*cell['depth'],'configured_ublk_queues':queues if target=='rublk' else None,'before':before,'after':after,'fio':job,'pilot':args.pilot,'protocol':VERSION,'job_ranges':job_ranges,'timing':json.loads((directory/'timing.json').read_text())};(directory/'metrics.json').write_text(json.dumps(report,indent=2)+'\n');manifest['results'].append(str(directory.relative_to(out)));(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('BENCH_CELL',target,repeat,index,flush=True)
     completed=True
    finally:stop(target,holder['server'],holder['log'],group_repeat,require_success=completed)
 assert manifest['program_sha256']=={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in [('fio','/usr/bin/fio'),('mdadm','/usr/sbin/mdadm'),('python3','/usr/bin/python3')]},'measurement tools changed during run'
 assert fingerprint()==manifest['full_source_sha256'],'source/harness changed during frozen run'
 if resume_evidence:
  from benchmark_resume import verify_imports
  verify_imports(resume_evidence)
 health.check()
 manifest['complete']=True
finally:
 # Clean only the array/worker created by this invocation on any failure.
 if Path('/sys/class/block/md1100/md').exists():subprocess.run(['/usr/sbin/mdadm','--stop',str(md)],capture_output=True,text=True,timeout=30)
 if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,timeout=30)
 if 'server' in globals() and server is not None and server.poll() is None:
  server.wait(timeout=30)
 manifest['finished_unix_ns']=time.time_ns();(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
 print('BENCHMARK_DIRECTORY',out,flush=True)
