#!/usr/bin/env python3
"""Independent per-LBA content oracle during online sync (not a benchmark).
Only the existing three authorized disposable QEMU member namespaces are reset.
"""
import argparse,hashlib,json,mmap,os,random,signal,struct,subprocess,sys,time
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--worker',type=int);parser.add_argument('--base',type=int);parser.add_argument('--seconds',type=int,default=40);parser.add_argument('--output',type=Path);args=parser.parse_args()
BLOCK=4096;COUNT=1024;NODE='/dev/ublkb1100'
def pattern(job,block,epoch):return struct.pack('<QQQQ',0x524149443543484b,job,block,epoch)*128
if args.worker is not None:
 jid=args.worker;rng=random.Random(2700+jid);epochs=[0]*COUNT;buf=mmap.mmap(-1,BLOCK);fd=os.open(NODE,os.O_RDWR|os.O_DIRECT);reads=writes=0;start=time.monotonic();deadline=start+args.seconds
 try:
  while time.monotonic()<deadline:
   i=rng.randrange(COUNT);off=args.base+i*BLOCK
   assert os.preadv(fd,[buf],off)==BLOCK and buf[:]==pattern(jid,i,epochs[i]),('content before write',jid,i,epochs[i]);reads+=1
   if rng.random()<.35:
    epochs[i]+=1;buf[:]=pattern(jid,i,epochs[i]);assert os.pwrite(fd,buf,off)==BLOCK;writes+=1
    assert os.preadv(fd,[buf],off)==BLOCK and buf[:]==pattern(jid,i,epochs[i]),('read-after-write',jid,i);reads+=1
  os.fsync(fd)
  for i,e in enumerate(epochs):assert os.preadv(fd,[buf],args.base+i*BLOCK)==BLOCK and buf[:]==pattern(jid,i,e),('final content',jid,i,e)
  args.output.write_text(json.dumps({'passed':True,'job':jid,'base':args.base,'epochs':epochs,'reads':reads,'writes':writes,'started_monotonic':start,'finished_monotonic':time.monotonic(),'verified_blocks':COUNT},indent=2)+'\n')
 finally:os.close(fd);buf.close()
 raise SystemExit(0)
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root);sys.path.insert(0,str(root/'scripts/raid5'));from nvme_members import resolve_members
from kernel_health import KernelHealthGuard
members=resolve_members();binary=Path(os.environ['RAID5_BINARY']).resolve();out=root/'docs/raid5/recovery-content'/str(time.time_ns());out.mkdir(parents=True);health=KernelHealthGuard(out)
assert not Path('/dev/ublkb1100').exists() and not Path('/sys/class/block/md1100/md').exists()
manifest={'complete':False,'scope':'QEMU NVMe content correctness, not performance or physical power loss','binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'members':list(map(str,members)),'worker_count':4,'blocks_per_worker':COUNT,'block_bytes':BLOCK,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()};logs=[];server=None;workers=[]
def save(): (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
def cmd(a,timeout=30):
 r=subprocess.run(a,capture_output=True,text=True,timeout=timeout);assert r.returncode==0,(a,r.stdout[-1000:],r.stderr[-1000:]);return r
def until(f,label,seconds=180):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  v=f()
  if v:return v
  assert server is None or server.poll() is None,'server exited '+label
  time.sleep(.02)
 raise AssertionError('timeout '+label)
def status():
 try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
 except (FileNotFoundError,ValueError):return False
 return s if s.get('pid')==server.pid else False
def start(label,assemble=False,rate='0'):
 log=(out/(label+'.log')).open('w');logs.append(log)
 return subprocess.Popen([str(binary),'add','raid5','-z','--foreground','-n','1100','-q','4','-d','64','--sync-window','8','--sync-rate',rate,*(['--assemble'] if assemble else []),*map(str,members)],stdout=log,stderr=log)
def stop():
 cmd([str(binary),'del','-n','1100']);assert server.wait(timeout=30)==0
save();print('RECOVERY_CONTENT_DIRECTORY',out,flush=True)
try:
 # Prepares only serial1/2/3; the reserved work disk is excluded by the resolver.
 r=cmd(['python3','scripts/raid5/prepare_nvme.py'],300);(out/'prepare.log').write_text(r.stdout+r.stderr)
 server=start('initialize');until(lambda:Path(NODE).exists() and (s:=status()) and s['state']=='RUNNING','initial RUNNING')
 capacity=int(cmd(['blockdev','--getsize64',NODE]).stdout);bases=[0,COUNT*BLOCK,capacity-2*COUNT*BLOCK,capacity-COUNT*BLOCK];manifest.update(capacity=capacity,bases=bases)
 buf=mmap.mmap(-1,BLOCK);fd=os.open(NODE,os.O_RDWR|os.O_DIRECT)
 for j,base in enumerate(bases):
  for i in range(COUNT):buf[:]=pattern(j,i,0);assert os.pwrite(fd,buf,base+i*BLOCK)==BLOCK
 os.fsync(fd)
 # Positive detector control: a legal frontend write keeps parity valid but
 # violates the independent expected content; parity alone cannot catch it.
 buf[:]=pattern(0,0,999999);assert os.pwrite(fd,buf,0)==BLOCK;os.fsync(fd)
 assert os.preadv(fd,[buf],0)==BLOCK and buf[:]!=pattern(0,0,0),'oracle failed to detect wrong content'
 manifest['content_detector_control']='unexpected legal write detected despite valid parity'
 buf[:]=pattern(0,0,0);assert os.pwrite(fd,buf,0)==BLOCK;os.fsync(fd)
 os.close(fd);buf.close();health.check()
 server.kill();server.wait(timeout=15);cmd([str(binary),'del','-n','1100']);manifest['crash']='SIGKILL after acknowledged initialization fsync; all members retained; not power loss'
 server=start('online-recovery',True,'64MiB');until(lambda:Path(NODE).exists() and (s:=status()) and s['state']=='RECOVERING','recovering')
 timeline=[];beg=time.monotonic();paused=resumed=False
 for j,base in enumerate(bases):
  log=(out/f'worker{j}.log').open('w');logs.append(log)
  workers.append(subprocess.Popen(['taskset','-c',str(2*j),sys.executable,str(Path(__file__).resolve()),'--worker',str(j),'--base',str(base),'--seconds',str(args.seconds),'--output',str(out/f'worker{j}.json')],stdout=log,stderr=log))
 while any(p.poll() is None for p in workers):
  s=status();assert s,'missing status';timeline.append({'monotonic':time.monotonic(),'status':s})
  elapsed=time.monotonic()-beg
  if elapsed>5 and not paused:
   (out/'pause.json').write_text(cmd([str(binary),'raid5-sync','-n','1100','--pause']).stdout);paused=True
  if elapsed>15 and not resumed:
   (out/'resume.json').write_text(cmd([str(binary),'raid5-sync','-n','1100','--resume']).stdout);resumed=True
  time.sleep(.05)
 assert all(p.wait(timeout=10)==0 for p in workers),[p.returncode for p in workers]
 assert paused and resumed and any(x['status']['sync']['paused'] for x in timeline),'pause was not observed'
 assert any(x['status']['sync']['active'] for x in timeline),'sync concurrency not observed'
 (out/'timeline.json').write_text(json.dumps(timeline,indent=2)+'\n')
 until(lambda:(s:=status()) and s['state']=='RUNNING','sync completion',240)
 # A second independent full read after sync completes uses saved per-LBA epochs.
 buf=mmap.mmap(-1,BLOCK);fd=os.open(NODE,os.O_RDONLY|os.O_DIRECT)
 for j,base in enumerate(bases):
  w=json.loads((out/f'worker{j}.json').read_text());assert w['passed']
  for i,e in enumerate(w['epochs']):assert os.preadv(fd,[buf],base+i*BLOCK)==BLOCK and buf[:]==pattern(j,i,e),('after sync',j,i)
 os.close(fd);buf.close();stop();health.check()
 r=cmd(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/check_physical.c','-o',str(out/'check-physical')]);r=cmd([str(out/'check-physical'),*map(str,members)],300);(out/'physical-parity.log').write_text(r.stdout+r.stderr)
 manifest.update(complete=True,content_check='every read and immediate read-after-write; all4096 blocks checked by workers and independently after sync',pause_resume=True,full_physical_parity=True,worker_results=[json.loads((out/f'worker{j}.json').read_text()) for j in range(4)])
 print('PASS recovery+IO independent content oracle',out,flush=True)
finally:
 for p in workers:
  if p.poll() is None:p.terminate();p.wait(timeout=10)
 if Path(NODE).exists():
  try:cmd([str(binary),'del','-n','1100'])
  finally:
   if server and server.poll() is None:server.wait(timeout=30)
 for log in logs:log.close()
 manifest['finished_ns']=time.time_ns();save()
