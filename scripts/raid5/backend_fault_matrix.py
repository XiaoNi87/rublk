#!/usr/bin/env python3
"""Real original-CQE faults via an external test-only FUSE file backend.
No production injection switch, no performance/zero-copy claim for FUSE storage.
"""
import argparse,json,os,shlex,subprocess,tempfile,time
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--case');args=parser.parse_args()
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
flags=shlex.split(subprocess.run(['pkg-config','--cflags','--libs','fuse3'],capture_output=True,text=True,check=True).stdout)
subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/fault_fs.c','-o','target/raid5-fault-fs',*flags],check=True)
assert not Path('/dev/ublkb1100').exists()
cases=[('read-error',0,'read','read','error',4096),('rmw-old-data-error',0,'read','write','error',4096),('rmw-old-parity-error',2,'read','write','error',4096),('rmw-data-error',0,'write','write','error',4096),('rmw-parity-error',2,'write','write','error',4096),('full-data0-error',0,'write','write','error',131072),('full-data1-error',1,'write','write','error',131072),('full-parity-error',2,'write','write','error',131072),('read-short',0,'read','read','short',4096),('rmw-old-data-short',0,'read','write','short',4096),('data-short',0,'write','write','short',4096),('parity-short',2,'write','write','short',4096),*[(f'flush{i}-error',i,'fsync','fsync','error',4096) for i in range(3)],('read-error-racing-stop',0,'read','read','hold-error',4096),('bpf-kfixed-iterator-error',0,'read','write','hold-success',4096)]
if args.case:cases=[c for c in cases if c[0]==args.case];assert cases,'unknown case'
out=root/'docs/raid5/backend-faults'/str(time.time_ns());out.mkdir(parents=True)
results=[]
client_code='''import errno,mmap,os,sys
fd=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,int(sys.argv[2]));buf[:]=bytes([165])*len(buf)
try:
 if sys.argv[1]=='read':os.preadv(fd,[buf],0)
 elif sys.argv[1]=='write':os.pwrite(fd,buf,0)
 else:os.fsync(fd)
except OSError as e:assert e.errno==errno.EIO,e;print('EXPECTED_EIO')
else:raise AssertionError('fault did not return EIO')
finally:os.close(fd);buf.close()
'''
for name,member,op,client_op,action,size in cases:
 directory=Path(tempfile.mkdtemp(prefix='raid5-backend-fault-',dir='/home/xiao'));backing=directory/'backing';backing.mkdir();mount=directory/'mount';mount.mkdir();control=directory/'control';paths=[mount/f'member{i}' for i in range(3)]
 for i in range(3):
  with (backing/f'member{i}').open('xb') as f:f.truncate(17*1048576)
 fslog=(out/f'{name}-fuse.log').open('w');fs=subprocess.Popen([str(root/'target/raid5-fault-fs'),str(mount),str(backing),str(control)],stdout=fslog,stderr=fslog);server=None;client=None;delete=None;log=None
 def start(assemble=False):
  global log
  log=(out/(name+'-'+('retry' if assemble else 'server')+'.log')).open('w')
  cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8']
  if assemble:cmd+=['--assemble']
  p=subprocess.Popen(cmd+list(map(str,paths)),stdout=log,stderr=log)
  for _ in range(1500):
   assert p.poll() is None,(out/f'{name}-server.log').read_text()
   try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
   except (ValueError,FileNotFoundError):s={}
   if s.get('pid')==p.pid and s.get('state')=='RUNNING':return p
   time.sleep(.01)
  raise AssertionError('startup timeout')
 try:
  for _ in range(500):
   assert fs.poll() is None
   if os.path.ismount(mount):break
   time.sleep(.01)
  else:raise AssertionError('FUSE mount timeout')
  server=start();control.write_text(f'{member} {op} {-1 if op=="fsync" else 1048576} {action}\n')
  client=subprocess.Popen(['python3','-c',client_code,client_op,str(size)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
  if action.startswith('hold-'):
   for _ in range(1000):
    if Path(str(control)+'.hit').exists():break
    assert client.poll() is None
    time.sleep(.01)
   else:raise AssertionError('held read not reached')
   if name=='bpf-kfixed-iterator-error':
    subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/clear_buffers.c','-o','target/raid5-clear-buffers'],check=True)
    rings=[]
    for info in Path(f'/proc/{server.pid}/fdinfo').iterdir():
     text=info.read_text()
     if any(line.startswith('SqMask:') and int(line.split(':')[1].strip(),0)==127 for line in text.splitlines()):rings.append(info.name)
    assert len(rings)==2,rings
    subprocess.run(['target/raid5-clear-buffers',str(server.pid),'8',*rings],check=True,capture_output=True)
   else:
    delete=subprocess.Popen([str(binary),'del','-n','1100'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True);time.sleep(.3)
    assert client.poll() is None and server.poll() is None and delete.poll() is None,'original CQE/resources released early'
   Path(str(control)+'.release').touch()
  text=client.communicate(timeout=30)[0];(out/f'{name}-client.log').write_text(text);assert client.returncode==0 and 'EXPECTED_EIO' in text,text
  assert Path(str(control)+'.hit').exists(),'fault not hit'
  if delete is not None:assert delete.communicate(timeout=30) is not None and delete.returncode==0
  server.wait(timeout=30);log.close()
  if name=='bpf-kfixed-iterator-error':assert 'RMW BPF XOR: CQE -14' in (out/f'{name}-server.log').read_text()
  if Path('/dev/ublkb1100').exists():subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30)
  for i in range(3):
   raw=(backing/f'member{i}').read_bytes()[:8192];assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in (0,4096)),(name,'failure incorrectly CLEAN',i)
  control.write_text('');server=start(assemble=True)
  subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30);assert server.wait(timeout=30)==0;log.close()
  files=[(backing/f'member{i}').read_bytes() for i in range(3)]
  for stripe in range(256):
   p=2-stripe%3;a=(p+1)%3;b=(p+2)%3;off=1048576+stripe*65536
   assert bytes(x^y for x,y in zip(files[a][off:off+65536],files[b][off:off+65536]))==files[p][off:off+65536],(name,stripe)
  results.append({'case':name,'member':member,'fault_op':op,'action':action,'original_cqe':'client EIO','failed_metadata':'all6 DIRTY','retry':'full sync and all256 physical stripes passed','fixture':str(directory)});(out/'manifest.json').write_text(json.dumps({'scope':'external FUSE test-file fault fixture, NOT production storage/performance','complete':False,'results':results},indent=2)+'\n');print('PASS',name,flush=True)
 finally:
  Path(str(control)+'.release').touch()
  if Path('/dev/ublkb1100').exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,timeout=30)
  if server is not None and server.poll() is None:server.wait(timeout=30)
  if log is not None and not log.closed:log.close()
  subprocess.run(['/usr/bin/fusermount3','-u',str(mount)],capture_output=True,timeout=20)
  if fs.poll() is None:fs.wait(timeout=20)
  fslog.close()
 subprocess.run(['python3','scripts/raid5/retire_backend_fixtures.py'],check=True,capture_output=True)
m=json.loads((out/'manifest.json').read_text());m['complete']=True;(out/'manifest.json').write_text(json.dumps(m,indent=2)+'\n');print('PASS',out,flush=True)
