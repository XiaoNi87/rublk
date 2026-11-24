#!/usr/bin/env python3
"""Real startup metadata failure gates, using external one-shot FUSE faults."""
import hashlib,json,mmap,os,shutil,subprocess,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root);assert os.geteuid()==0
binary=Path(os.environ['RAID5_BINARY']).resolve();assert binary.exists()
assert not list(Path('/dev').glob('ublkb1100'))
out=root/'docs/raid5/metadata-startup-faults'/str(time.time_ns());out.mkdir(parents=True)
fixture=Path(tempfile.mkdtemp(prefix='raid5-metadata-startup-',dir='/home/xiao'));baseline=fixture/'baseline';baseline.mkdir()
for i in range(3):
 with (baseline/f'member{i}').open('xb') as f:f.truncate(17*1048576)
results=[];server=None;fs=None;log=None
manifest={'complete':False,'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'scope':'external FUSE failures in real service startup; process/cache-preserving verification, not power loss','cases':results}
def save():
 (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
def paths(directory):return [directory/f'member{i}' for i in range(3)]
def start(directory,label,assemble=False):
 global log,server
 log=(out/(label+'.log')).open('w');command=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8']
 if assemble:command+=['--assemble']
 server=subprocess.Popen(command+list(map(str,paths(directory))),stdout=log,stderr=log)
 return server
def ready(label):
 for _ in range(3000):
  assert server.poll() is None,(out/(label+'.log')).read_text()
  try:j=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
  except (FileNotFoundError,ValueError):j={}
  if j.get('pid')==server.pid and j.get('state')=='RUNNING' and Path('/dev/ublkb1100').exists():
   r=subprocess.run([str(binary),'list','-n','1100'],capture_output=True,text=True)
   if 'state LIVE' in r.stdout:return j
  time.sleep(.01)
 raise AssertionError('startup did not become RUNNING/LIVE')
def stop():
 global server,log
 if Path('/sys/class/ublk-char/ublkc1100').exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,check=True,timeout=30)
 if server is not None:assert server.wait(timeout=30)==0;server=None
 if log is not None:log.close();log=None
 for _ in range(1000):
  if not Path('/dev/ublkb1100').exists() and not Path('/sys/class/ublk-char/ublkc1100').exists():break
  time.sleep(.01)
 else:raise AssertionError('device deletion timeout')
def crc32c(data):
 crc=0xffffffff
 for b in data:
  crc^=b
  for _ in range(8):crc=(crc>>1)^(0x82f63b78 if crc&1 else 0)
 return crc^0xffffffff
def records(directory):
 rows=[]
 for i,p in enumerate(paths(directory)):
  with p.open('rb') as f:prefix=f.read(8192)
  slots=[]
  for offset in (0,4096):
   raw=bytearray(prefix[offset:offset+4096]);expected=int.from_bytes(raw[16:20],'little');raw[16:20]=b'\0'*4
   valid=raw[:8]==b'RUBLK5\0\0' and crc32c(raw)==expected
   slots.append({'valid':valid,'array':raw[24:40].hex(),'member':int.from_bytes(raw[88:92],'little'),'generation':int.from_bytes(raw[128:136],'little'),'state':int.from_bytes(raw[20:24],'little')})
  assert any(s['valid'] and s['member']==i for s in slots),(i,slots)
  rows.append(slots)
 return rows
def data_hashes(directory):
 result=[]
 for p in paths(directory):
  with p.open('rb') as f:f.seek(1048576);result.append(hashlib.file_digest(f,'sha256').hexdigest())
 return result
try:
 start(baseline,'baseline');ready('baseline')
 fd=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,131072)
 try:
  for stripe in range(16):
   buf[:]=bytes((stripe*31+j*7+(j>>10))%256 for j in range(len(buf)));assert os.pwrite(fd,buf,stripe*131072)==len(buf)
  os.fsync(fd)
 finally:os.close(fd);buf.close()
 stop();initial=records(baseline);expected=data_hashes(baseline);identity=initial[0][0]['array']
 cases=[(f'baseline-flush-m{m}',m,'fsync',-1,'error') for m in range(3)]
 cases += [(f'slot{slot}-{action}-m{m}',m,'write',slot*4096,action) for slot in (0,1) for action in ('error','short') for m in range(3)]
 subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/fault_fs.c','-o','target/raid5-fault-fs',*subprocess.run(['pkg-config','--cflags','--libs','fuse3'],capture_output=True,text=True,check=True).stdout.split()],check=True)
 for name,member,operation,offset,action in cases:
  directory=fixture/name;directory.mkdir();backing=directory/'backing';backing.mkdir();mount=directory/'mount';mount.mkdir();control=directory/'control'
  for a,b in zip(paths(baseline),paths(backing)):subprocess.run(['cp','--sparse=always',str(a),str(b)],check=True)
  fslog=(out/(name+'-fuse.log')).open('w');fs=subprocess.Popen(['target/raid5-fault-fs',str(mount),str(backing),str(control)],stdout=fslog,stderr=fslog)
  try:
   for _ in range(1000):
    assert fs.poll() is None
    if os.path.ismount(mount):break
    time.sleep(.01)
   else:raise AssertionError('FUSE mount timeout')
   control.write_text(f'{member} {operation} {offset} {action}\n');start(mount,name+'-failure',True)
   exit_code=server.wait(timeout=30);server=None;log.close();log=None
   text=(out/(name+'-failure.log')).read_text();assert exit_code!=0 and Path(str(control)+'.hit').exists(),(name,exit_code,text)
   assert 'RAID5 STARTING' not in text and not Path('/sys/class/ublk-char/ublkc1100').exists(),(name,'published before metadata admission')
   survivor=records(backing)
   assert all(any(s['valid'] and s['array']==identity for s in row) for row in survivor)
   assert data_hashes(backing)==expected,(name,'startup failure modified data')
   control.write_text('');start(mount,name+'-retry',True);ready(name+'-retry');stop()
   assert data_hashes(backing)==expected,(name,'retry changed data')
   final=records(backing);assert all(all(s['valid'] and s['state']==1 and s['array']==identity for s in row) for row in final)
   files=[p.read_bytes() for p in paths(backing)]
   for stripe in range(256):
    parity=2-stripe%3;a=(parity+1)%3;b=(parity+2)%3;off=1048576+stripe*65536
    assert bytes(x^y for x,y in zip(files[a][off:off+65536],files[b][off:off+65536]))==files[parity][off:off+65536],(name,stripe)
   results.append({'name':name,'member':member,'operation':operation,'offset':offset,'action':action,'startup_exit':exit_code,'frontend_published':False,'survivors':survivor,'data_sha256':expected,'retry':'RUNNING, unchanged data, all256 independent parity stripes, six CLEAN records'});save();print('PASS',name,flush=True)
  finally:
   if server is not None:
    if server.poll() is None:stop()
    else:server=None
   if log is not None:log.close();log=None
   subprocess.run(['fusermount3','-u',str(mount)],capture_output=True,timeout=20)
   if fs is not None:assert fs.wait(timeout=20)==0;fs=None
   fslog.close()
  shutil.rmtree(directory)
 manifest.update(complete=True,finished_unix_ns=time.time_ns());save();print('PASS',out,flush=True)
finally:
 if server is not None and server.poll() is None:stop()
 if fs is not None and fs.poll() is None:fs.terminate();fs.wait(timeout=20)
 if manifest.get('complete'):shutil.rmtree(fixture)
