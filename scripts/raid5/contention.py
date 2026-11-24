#!/usr/bin/env python3
"""Optional-instrumented contention diagnostic; separate from throughput acceptance."""
import argparse,atexit,hashlib,json,os,subprocess,tempfile,time
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--binary',default='target/release/rublk');parser.add_argument('--coord',default='target/release/examples/raid5_coord_bench');parser.add_argument('--seconds',type=int,default=2);parser.add_argument('--queues',default='1,4');parser.add_argument('--shards',default='1,16,64,256');parser.add_argument('--depth',type=int,default=16);parser.add_argument('--bs',default='4k');parser.add_argument('--skip-coord',action='store_true');parser.add_argument('--bpf-stats',action='store_true');args=parser.parse_args();assert args.seconds>0 and 1<=args.depth<=128
root=Path(__file__).resolve().parents[2];os.chdir(root)
assert os.geteuid()==0
binary=(root/args.binary).resolve();coord=(root/args.coord).resolve()
assert not Path('/dev/ublkb1100').exists()
stats=Path('/proc/sys/kernel/bpf_stats_enabled');previous_stats=stats.read_text().strip() if stats.exists() else None
if args.bpf_stats:
 assert previous_stats is not None,'kernel BPF statistics unavailable'
 stats.write_text('1')
 atexit.register(lambda:stats.write_text(previous_stats))
def bpf_programs():
 rows=json.loads(subprocess.run(['/usr/sbin/bpftool','-j','prog','show'],capture_output=True,text=True,check=True).stdout)
 return {r['id']:r for r in rows if r.get('name','').startswith('xor_')}

out=root/'docs/raid5/contention'/str(time.time_ns());out.mkdir(parents=True)
manifest={'scope':'instrumented file-backed diagnostic; NOT uninstrumented physical NVMe performance','binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'results':[],'complete':False,'arguments':vars(args),'previous_bpf_stats_enabled':previous_stats,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
from source_manifest import fingerprint
manifest['full_source_sha256']=fingerprint()
manifest['coord_sha256']=hashlib.sha256(coord.read_bytes()).hexdigest() if coord.exists() else None
# Diagnostic duration is explicit; these are not paired throughput acceptance numbers:
# Pilot duration deliberately short: validate measurement and queue progress only.
for threads in (() if args.skip_coord else (1,4)):
 for pattern in ('gate','disjoint','hot'):
  for shards in ((64,) if pattern=='gate' else (1,16,64,256)):
   for flush_every in (0,100):
    name=f'coord-{threads}-{pattern}-{shards}-{flush_every}';r=subprocess.run([str(coord),str(shards),str(threads),pattern,'1',str(flush_every)],capture_output=True,text=True,timeout=20);(out/f'{name}.log').write_text(r.stdout+r.stderr);assert r.returncode==0,r.stderr
    manifest['results'].append({'name':name,'kind':'coordination-only','result':json.loads(r.stdout)})
directory=Path(tempfile.mkdtemp(prefix='raid5-contention-',dir='/home/xiao'));members=[directory/f'member{i}' for i in range(3)]
for p in members:
 with p.open('xb') as f:f.truncate(17*1048576)
block_size=int(args.bs[:-1])*{'k':1024,'m':1048576}[args.bs[-1].lower()] if args.bs[-1].lower() in ('k','m') else int(args.bs)
hot_span=max(131072,block_size)
first=True
for queues in map(int,args.queues.split(',')):
 for shards in map(int,args.shards.split(',')):
  for pattern in ('disjoint','hot-read','hot-mixed','hot-flush'):
   name=f'target-{queues}-{shards}-{pattern}-d{args.depth}-{args.bs}';log=(out/f'{name}.log').open('w');p=None
   try:
    cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q',str(queues),'-d',str(args.depth),'--stripe-shards',str(shards)]
    if not first:cmd+=['--assemble']
    p=subprocess.Popen(cmd+list(map(str,members)),stdout=log,stderr=log)
    for _ in range(1000):
     assert p.poll() is None,(out/f'{name}.log').read_text()
     try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
     except (ValueError,FileNotFoundError):s={}
     if s.get('pid')==p.pid and s.get('state')=='RUNNING':break
     time.sleep(.01)
    else:raise AssertionError('startup timeout')
    first=False
    fio=['fio','--name='+name,'--filename=/dev/ublkb1100','--rw='+('randread' if pattern=='hot-read' else 'randrw'),'--rwmixread=70','--bs='+args.bs,'--ioengine=io_uring','--direct=1','--iodepth='+str(args.depth),'--numjobs=4','--size='+('8388608' if pattern=='disjoint' else str(hot_span)),'--offset_increment='+('8388608' if pattern=='disjoint' else '0'),'--runtime='+str(args.seconds),'--time_based=1','--cpus_allowed=0,2,4,6','--cpus_allowed_policy=split','--output-format=json+','--output='+str(out/f'{name}-fio.json')]
    if pattern=='hot-flush':fio+=['--fsync=32']
    before_bpf=bpf_programs() if args.bpf_stats else {}
    r=subprocess.run(fio,capture_output=True,text=True,timeout=args.seconds+60);(out/f'{name}-fio.log').write_text(r.stdout+r.stderr);assert r.returncode==0,r.stderr
    if args.bpf_stats:
     after_bpf=bpf_programs();deltas=[]
     for pid,before in before_bpf.items():
      after=after_bpf.get(pid)
      if after is not None:deltas.append({'id':pid,'name':before['name'],'run_time_ns_delta':after.get('run_time_ns',0)-before.get('run_time_ns',0),'run_cnt_delta':after.get('run_cnt',0)-before.get('run_cnt',0),'before_runtime_field_present':'run_time_ns' in before,'after_runtime_field_present':'run_time_ns' in after,'runtime_observed':'run_time_ns' in after})
     (out/f'{name}-bpf-cpu.json').write_text(json.dumps({'method':'kernel BPF statistics, separate instrumented diagnostic','previous_bpf_stats_enabled':previous_stats,'zero_field_semantics':'bpftool prog.c emits run_time_ns/run_cnt only when run_time_ns != 0; absent zero before maps to zero, positive after establishes observation','program_deltas':deltas},indent=2)+'\n')
   finally:
    if Path('/dev/ublkb1100').exists():subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30)
    if p is not None:assert p.wait(timeout=20)==0
    log.close()
   rows=[json.loads(line.split('RAID5_METRICS ',1)[1]) for line in (out/f'{name}.log').read_text().splitlines() if 'RAID5_METRICS ' in line]
   normal=[r for r in rows if r['label'].startswith('queue-')];assert len(normal)==queues,rows
   assert all(sum(r['histograms']['request'])>100 for r in normal),[(r['label'],sum(r['histograms']['request'])) for r in normal]
   assert all(r['backend_bpf_cqe_progress_elapsed_ns'][-1][1]>0 for r in normal)
   (out/f'{name}-metrics.json').write_text(json.dumps(rows,indent=2)+'\n')
   # Independent physical parity after each quiesced cell, never userspace XOR fallback.
   files=[m.read_bytes() for m in members]
   for stripe in range(256):
    parity=2-stripe%3;d0=(parity+1)%3;d1=(parity+2)%3;off=1048576+stripe*65536
    assert bytes(a^b for a,b in zip(files[d0][off:off+65536],files[d1][off:off+65536]))==files[parity][off:off+65536]
   manifest['results'].append({'name':name,'kind':'file-backed target','queue_count':queues,'shards':shards,'pattern':pattern,'hot_span_bytes':hot_span,'metrics':len(rows),'physical_parity':'all256 stripes passed'})
   (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('PASS',name,flush=True)
manifest['complete']=True;manifest['fixture']=str(directory);(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print('PASS',out,flush=True)

hashes={}
for i,p in enumerate(members):
 data=p.read_bytes();hashes[str(i)]=hashlib.sha256(data).hexdigest();(out/f'final-member{i}-metadata.bin').write_bytes(data[:12288]);p.unlink()
manifest.update(fixture_retired=True,retired_fixture_sha256=hashes)
(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
