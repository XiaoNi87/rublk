#!/usr/bin/env python3
"""External arena allocation failure and rollback acceptance on disposable files."""
from pathlib import Path
import hashlib,json,os,subprocess,time,mmap,shutil
assert os.geteuid()==0
R=Path(__file__).resolve().parents[2];W=Path(os.environ.get('RAID5_WORK','/home/xiao/rublk-raid5-work'));D=R/'docs/raid5/arena-population'/str(time.time_ns());D.mkdir(parents=True);B=Path(os.environ['RAID5_BINARY']).resolve();L=D/'populate_fault.so';P=Path(os.environ.get('RAID5_ARENA_PROBE',str(W/'target/release/examples/raid5_arena_probe')));device='1103';node=Path('/dev/ublkb'+device);sysnode=Path('/sys/class/ublk-char/ublkc'+device)
subprocess.run(['cc','-shared','-fPIC','-O2','-Wall','-Wextra','-Werror',str(R/'scripts/raid5/populate_fault.c'),'-o',str(L),'-ldl'],check=True)
assert not sysnode.exists();before={m['id'] for m in json.loads(subprocess.check_output(['bpftool','-j','map','show']))};results=[]
def wait(fn):
 end=time.monotonic()+30
 while not fn():
  assert time.monotonic()<end,'timeout';time.sleep(.02)
def maps_clear():return not ({m['id'] for m in json.loads(subprocess.check_output(['bpftool','-j','map','show']))}-before)
for mode in ['once','all']:
 e={**os.environ,'LD_PRELOAD':str(L),'RAID5_TEST_POPULATE':mode,'TMPDIR':str(W/'tmp')};t=time.monotonic();p=subprocess.run([str(P)],env=e,capture_output=True,text=True,timeout=30);(D/('probe-'+mode+'.log')).write_text(p.stdout+p.stderr)
 assert (p.returncode==0 if mode=='once' else p.returncode>0),(p.returncode,p.stderr)
 if mode=='all':assert p.stderr.count('INJECT_POPULATE_EFAULT')==8,p.stderr
 wait(maps_clear);results.append({'case':'probe-'+mode,'exit':p.returncode,'seconds':time.monotonic()-t})
# A failed log write must not turn a recoverable allocation failure into panic.
rfd,wfd=os.pipe();os.close(rfd)
try:
 e={**os.environ,'LD_PRELOAD':str(L),'RAID5_TEST_POPULATE':'once','TMPDIR':str(W/'tmp')};p=subprocess.run([str(P)],env=e,stdout=subprocess.PIPE,stderr=wfd,text=True,timeout=30);assert p.returncode==0,p.stdout
finally:os.close(wfd)
wait(maps_clear);results.append({'case':'probe-once-broken-stderr','exit':p.returncode})
work=D/'members';work.mkdir();members=[work/str(i) for i in range(3)]
for p in members:
 with p.open('xb') as f:f.truncate(5*1048576)
base=[str(B),'add','raid5','-z','--test-files','--foreground','-n',device,'-q','4','-d','128']
def start(name,mode=None,assemble=False):
 f=(D/(name+'.log')).open('w');env=os.environ.copy()
 if mode:env.update(LD_PRELOAD=str(L),RAID5_TEST_POPULATE=mode)
 p=subprocess.Popen([*base,*(['--assemble'] if assemble else []),*map(str,members)],env=env,stdout=f,stderr=f)
 return p,f
def running(p):
 assert p.poll() is None,(p.returncode,(D/'service-once.log').read_text())
 try:s=json.loads(Path('/run/rublk-raid5/raid5-'+device+'.status.json').read_text())
 except (FileNotFoundError,ValueError):return False
 return s.get('pid')==p.pid and s['state']=='RUNNING'
p,f=start('prepare');wait(lambda:running(p));subprocess.run([str(B),'del','-n',device],check=True,capture_output=True,timeout=20);assert p.wait(timeout=20)==0;f.close();wait(lambda:not sysnode.exists());wait(maps_clear)
p,f=start('service-all','all',True);t=time.monotonic();rc=p.wait(timeout=30);f.close();assert rc>0;wait(lambda:not sysnode.exists());wait(maps_clear)
for m in members:
 raw=m.read_bytes()[:8192];assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096]),'incorrect CLEAN'
results.append({'case':'service-persistent-failure','exit':rc,'seconds':time.monotonic()-t,'all_six_slots_dirty':True,'no_device_or_extra_maps':True})
p,f=start('service-once','once',True)
try:
 wait(lambda:running(p));buf=mmap.mmap(-1,4096);buf[:]=bytes([0x59])*4096;fd=os.open(node,os.O_RDWR|os.O_DIRECT);assert os.pwrite(fd,buf,4096)==4096;os.fsync(fd);buf[:]=bytes(4096);assert os.preadv(fd,[buf],4096)==4096 and buf[:]==bytes([0x59])*4096;os.close(fd);buf.close();subprocess.run([str(B),'del','-n',device],check=True,capture_output=True,timeout=20);assert p.wait(timeout=20)==0;wait(lambda:not sysnode.exists());wait(maps_clear)
finally:
 if sysnode.exists():subprocess.run([str(B),'del','-n',device],capture_output=True,timeout=20)
 if p.poll() is None:p.wait(timeout=20)
 f.close()
a=[m.read_bytes() for m in members]
for raw in a:assert all(int.from_bytes(raw[o+20:o+24],'little')==1 for o in [0,4096])
for stripe in range(64):
 p=2-stripe%3;i,j=(p+1)%3,(p+2)%3;off=1048576+stripe*65536;assert bytes(x^y for x,y in zip(a[i][off:off+65536],a[j][off:off+65536]))==a[p][off:off+65536]
results.append({'case':'service-transient-then-recovery','exit':0,'physical_stripes':64,'direct_write_read_flush_clean':True,'no_device_or_extra_maps':True})
shutil.copy2(__file__,D/'test.py');shutil.copy2(R/'scripts/raid5/populate_fault.c',D/'populate_fault.c')
manifest={'complete':True,'binary':str(B),'binary_sha256':hashlib.sha256(B.read_bytes()).hexdigest(),'probe_sha256':hashlib.sha256(P.read_bytes()).hexdigest(),'injector_sha256':hashlib.sha256(L.read_bytes()).hexdigest(),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'results':results};(D/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print(json.dumps(manifest,indent=2),flush=True);print('DIRECTORY',D,flush=True)
