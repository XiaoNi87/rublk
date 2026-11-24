#!/usr/bin/env python3
"""Real RAID5 file-fixture correctness/lifecycle; never physical-NVMe performance."""
import json,mmap,os,signal,subprocess,tempfile,time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
assert os.geteuid()==0,'root required; never skip'
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
C=65536;PREFIX=1048576;STRIPES=256;CAP=STRIPES*2*C
node=Path('/dev/ublkb1100');assert not node.exists()
directory=Path(tempfile.mkdtemp(prefix='raid5-target-test-',dir='/home/xiao'))
members=[directory/f'member{i}' for i in range(3)]
for p in members:
 with p.open('xb') as f:f.truncate(PREFIX+STRIPES*C)
expected=bytearray(CAP);cases=[];servers=[];logs=[];generation=0
status_path=Path('/run/rublk-raid5/raid5-1100.status.json')
def pattern(offset,size,salt):return bytes(((i*13+17)^((i>>8)*29)^((i>>12)*71)^((offset>>9)%251)^salt)&255 for i in range(size))
def status():return json.loads(status_path.read_text())
def start(assemble=False,rate='0'):
 global generation
 logpath=root/'docs/raid5'/f'target-integration-session-{len(servers)}.log';log=logpath.open('w');logs.append(log)
 cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8','-b','1MiB','--sync-rate',rate]
 if assemble:cmd+=['--assemble']
 p=subprocess.Popen(cmd+list(map(str,members)),stdout=log,stderr=log);servers.append(p)
 for _ in range(800):
  if p.poll() is not None:raise RuntimeError(logpath.read_text())
  if node.exists() and status_path.exists():
   s=status()
   if s['pid']==p.pid and s['state'] in ('RUNNING','RECOVERING'):break
  time.sleep(.01)
 else:raise AssertionError(logpath.read_text())
 generation=s['generation'];print('START',s,flush=True);return p

def write(offset,size,salt=0):
 data=pattern(offset,size,salt);fd=os.open(node,os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,size);buf[:]=data
 assert os.pwrite(fd,buf,offset)==size;os.fsync(fd)
 read=mmap.mmap(-1,size);assert os.preadv(fd,[read],offset)==size and read[:]==data
 os.close(fd);buf.close();read.close();expected[offset:offset+size]=data;cases.append({'offset':offset,'size':size,'salt':salt})

def verify_logical():
 fd=os.open(node,os.O_RDONLY|os.O_DIRECT);buf=mmap.mmap(-1,2*C)
 for off in range(0,CAP,2*C):assert os.preadv(fd,[buf],off)==2*C and buf[:]==expected[off:off+2*C],f'logical mismatch at {off}'
 os.close(fd);buf.close();print('PASS complete logical content oracle',flush=True)

def verify_physical():
 files=[p.read_bytes() for p in members]
 for stripe in range(STRIPES):
  parity=[2,1,0][stripe%3];d0=(parity+1)%3;d1=(parity+2)%3;off=PREFIX+stripe*C
  a=files[d0][off:off+C];b=files[d1][off:off+C];p=files[parity][off:off+C]
  assert a+b==expected[stripe*2*C:(stripe+1)*2*C],('physical logical ordering',stripe)
  assert bytes(x^y for x,y in zip(a,b))==p,('physical parity',stripe)
 print('PASS all physical data/parity stripes',flush=True)

def metadata(clean,gen):
 for p in members:
  with p.open('rb') as f:raw=f.read(12288)
  for slot in (0,4096):
   b=bytearray(raw[slot:slot+4096]);stored=int.from_bytes(b[16:20],'little');b[16:20]=bytes(4);crc=0xffffffff
   for byte in b:
    crc^=byte
    for _ in range(8):crc=(crc>>1)^(0x82f63b78 if crc&1 else 0)
   assert stored==(crc^0xffffffff),'metadata CRC mismatch'
   assert int.from_bytes(b[20:24],'little')==(1 if clean else 2)
   assert int.from_bytes(b[128:136],'little')==gen
 print('PASS six metadata slots',{'clean':clean,'generation':gen},flush=True)

def stop(p,clean):
 r=subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,timeout=20);assert r.returncode==0,(r.stdout,r.stderr)
 p.wait(timeout=20);assert p.returncode==0
 s=status();assert s['state']==('STOPPED_CLEAN' if clean else 'STOPPED_DIRTY'),s
 metadata(clean,generation+1 if clean else generation)
 assert not node.exists()

def kill(p):
 os.kill(p.pid,signal.SIGKILL);p.wait(timeout=15)
 subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,timeout=20,check=True)
 assert not node.exists();metadata(False,generation)

def wait_synced():
 for _ in range(2000):
  s=status()
  if s['state']=='RUNNING' and s['sync']['frontier']==STRIPES:return s
  assert s['state']!='ERROR',s;time.sleep(.01)
 raise AssertionError(s)
try:
 p=start()
 for off,size in [(0,2*C),(2*C,2*C),(4*C,2*C),(8192,4096),(C-4096,12288),(C,4*C),(512,512),(C-512,1024),(2*C-512,1024),(12*1048576,1048576)]:write(off,size)
 # CPUs on distinct normal queues race same parity bytes on opposite data disks.
 allowed=sorted(os.sched_getaffinity(0));assert len(allowed)>=8,allowed
 cpus=[allowed[i] for i in (0,2,4,6)]
 def hot_writer(worker):
  os.sched_setaffinity(0,{cpus[worker%len(cpus)]})
  offset=40*2*C+(worker%2)*C+(worker//2)*4096
  for iteration in range(80):write(offset,4096,(worker*53+iteration)%256)
 with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(hot_writer,range(4)))
 print('PASS four CPU-pinned writers: same-stripe conflicting parity updates and FLUSH',flush=True)
 verify_logical();stop(p,True);verify_physical()
 p=start(True);assert status()['sync']['started_unix_ns']==0,'CLEAN assembly unexpectedly syncing';verify_logical();stop(p,True)
 # Crash an established session; retain only DIRTY metadata. Corrupt parity
 # independently to model stale parity without changing available data members.
 p=start(True);write(12*C,4096,77);kill(p)
 for stripe in range(STRIPES):
  parity=[2,1,0][stripe%3]
  with members[parity].open('r+b',buffering=0) as f:f.seek(PREFIX+stripe*C);f.write(bytes([0xdd])*C);os.fsync(f.fileno())
 p=start(True,'16MiB');s=status();assert s['sync']['active'] and s['sync']['frontier']<200,s
 def control(*args):
  r=subprocess.run([str(binary),'raid5-sync','-n','1100',*args],capture_output=True,text=True,timeout=15);assert r.returncode==0,(r.stdout,r.stderr);return json.loads(r.stdout)
 s=control('--pause');assert s['sync']['paused'] and not s['sync']['active'],s
 f=s['sync']['frontier'];time.sleep(.15);assert status()['sync']['frontier']==f,'paused scanner advanced'
 s=control('--rate','8MiB');assert s['sync']['rate_bytes_per_sec']==8*1048576
 # Root-private channel rejects stale identity without changing control state.
 import socket
 stream=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);stream.connect('/run/rublk-raid5/raid5-1100.status.control.sock')
 stream.sendall(json.dumps({'uuid':'foreign','generation':s['generation'],'pid':p.pid,'action':'Resume'}).encode());stream.shutdown(socket.SHUT_WR)
 assert json.loads(stream.recv(4096))['accepted']==False;stream.close();assert status()['sync']['pause_requested']

 # Far beyond F: repair and RMW must remain one stripe/admission transaction.
 write(200*2*C+8192,4096,91)
 # Already completed prefix plus full-stripe and cross-boundary writes during sync.
 write(0,4096,92);write(210*2*C,2*C,93);write(220*2*C+C-4096,12288,94)
 s=control('--resume');assert not s['sync']['pause_requested'];control('--rate','16MiB')
 completed=wait_synced();assert completed['sync']['completed_unix_ns']>=completed['sync']['finalization_started_unix_ns']>=completed['sync']['scan_completed_unix_ns']>=completed['sync']['started_unix_ns']>0
 verify_logical();stop(p,True);verify_physical();print('PASS full sync concurrent with normal reads/writes/RMW and FLUSH',flush=True)
 # An interrupted sync must remain DIRTY; a new dirty session starts from zero.
 p=start(True);kill(p);p=start(True,'1MiB');s=status();assert s['sync']['frontier']<STRIPES;control('--pause');stop(p,False)
 p=start(True);s=wait_synced();verify_logical();stop(p,True);verify_physical()
 print('PASS interrupted sync stays DIRTY and subsequent full sync succeeds',flush=True)
 # Retain the QUIESCED ublk device and recover through persisted discovery JSON.
 p=start(True);write(234*2*C+4096,4096,109)
 os.kill(p.pid,signal.SIGKILL);p.wait(timeout=15);metadata(False,generation)
 old_pid=p.pid
 # Process wait is not kernel quiescence: queue death/fput completes asynchronously.
 for _ in range(1000):
  inspect=subprocess.run([str(binary),'list','-n','1100'],capture_output=True,text=True,timeout=5)
  if 'state QUIESCED' in inspect.stdout:break
  time.sleep(.01)
 else:raise AssertionError(inspect.stdout+inspect.stderr)
 r=subprocess.run([str(binary),'recover','-n','1100'],capture_output=True,text=True,timeout=20)
 assert r.returncode==0,(r.stdout,r.stderr)
 for _ in range(2000):
  s=status()
  if s['pid']!=old_pid and s['state'] in ('RUNNING','RECOVERING'):break
  time.sleep(.01)
 else:raise AssertionError(s)
 generation=s['generation'];assert generation>0
 recovered_pid=s['pid'];assert Path(f'/proc/{recovered_pid}/exe').resolve()==binary
 wait_synced();verify_logical();write(235*2*C+8192,4096,110)
 subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,text=True,timeout=20)
 for _ in range(2000):
  s=status()
  if s['pid']==recovered_pid and s['state']=='STOPPED_CLEAN':break
  time.sleep(.01)
 else:raise AssertionError(s)
 metadata(True,generation+1);verify_physical()
 print('PASS retained ublk recover: new worker, original identity, full sync and flushed logical epoch',flush=True)

finally:
 if node.exists():subprocess.run(['timeout','15',str(binary),'del','-n','1100'],capture_output=True,text=True)
 for p in servers:
  if p.poll() is None:p.kill()
  p.wait(timeout=15)
 for log in logs:log.close()
 report={'fixture_directory':str(directory),'members':list(map(str,members)),'cases':cases,'capacity':CAP,'stripes':STRIPES,'all_servers_exited':[p.returncode for p in servers]}
 (root/'docs/raid5/target-integration-fixture.json').write_text(json.dumps(report,indent=2)+'\n')
 assert not node.exists()
