#!/usr/bin/env python3
"""External original-CQE failures in the background scanner/finalizer, no target hooks."""
import json,os,shlex,signal,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
flags=shlex.split(subprocess.run(['pkg-config','--cflags','--libs','fuse3'],capture_output=True,text=True,check=True).stdout)
subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/fault_fs.c','-o','target/raid5-fault-fs',*flags],check=True)
node=Path('/dev/ublkb1100');assert not node.exists()
out=root/'docs/raid5/backend-faults'/str(time.time_ns());out.mkdir(parents=True);results=[]
cases=[('sync-data-read-error',0,'read',1048576+255*65536),('sync-parity-write-error',2,'write',1048576+255*65536),*[(f'sync-finalizer-flush{i}-error',i,'fsync',-1) for i in range(3)]]
for name,member,operation,offset in cases:
 directory=Path(tempfile.mkdtemp(prefix='raid5-backend-fault-',dir='/home/xiao'));backing=directory/'backing';backing.mkdir();mount=directory/'mount';mount.mkdir();control=directory/'control';paths=[mount/f'member{i}' for i in range(3)]
 for i in range(3):
  with (backing/f'member{i}').open('xb') as f:f.truncate(17*1048576)
 fslog=(out/f'{name}-fuse.log').open('w');fs=subprocess.Popen(['target/raid5-fault-fs',str(mount),str(backing),str(control)],stdout=fslog,stderr=fslog);server=None;logs=[]
 def start(label,assemble=False,rate='0',recovering=False):
  log=(out/f'{name}-{label}.log').open('w');logs.append(log)
  cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8','--sync-rate',rate]
  if assemble:cmd+=['--assemble']
  p=subprocess.Popen(cmd+list(map(str,paths)),stdout=log,stderr=log)
  for _ in range(1500):
   assert p.poll() is None,(out/f'{name}-{label}.log').read_text()
   try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
   except (FileNotFoundError,ValueError):s={}
   if s.get('pid')==p.pid and s.get('state')==('RECOVERING' if recovering else 'RUNNING'):return p,s
   time.sleep(.01)
  raise AssertionError('startup timeout')
 try:
  for _ in range(500):
   assert fs.poll() is None
   if os.path.ismount(mount):break
   time.sleep(.01)
  else:raise AssertionError('FUSE mount timeout')
  server,_=start('prepare');server.kill();server.wait(timeout=15)
  subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30)
  server,snapshot=start('fault',True,'1MiB',True)
  assert snapshot['sync']['active'] and snapshot['sync']['frontier']<128,snapshot
  (out/f'{name}-armed-status.json').write_text(json.dumps(snapshot,indent=2)+'\n')
  temporary=control.with_suffix('.tmp');temporary.write_text(f'{member} {operation} {offset} error\n');os.replace(temporary,control)
  subprocess.run([str(binary),'raid5-sync','-n','1100','--rate','16MiB'],check=True,capture_output=True,timeout=15)
  server.wait(timeout=45);assert server.returncode!=0,'background error incorrectly succeeded'
  assert Path(str(control)+'.hit').exists(),'background fault not hit'
  if node.exists():subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30)
  for i in range(3):
   data=(backing/f'member{i}').read_bytes()[:8192]
   assert all(int.from_bytes(data[o+20:o+24],'little')==2 for o in (0,4096)),(name,'incorrect CLEAN')
  text=(out/f'{name}-fault.log').read_text()
  if operation=='fsync':assert 'sync finalization FLUSH: CQE -5' in text,text
  control.write_text('');server,_=start('retry',True)
  subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30);assert server.wait(timeout=30)==0
  files=[(backing/f'member{i}').read_bytes() for i in range(3)]
  for stripe in range(256):
   p=2-stripe%3;a=(p+1)%3;b=(p+2)%3;off=1048576+stripe*65536
   assert bytes(x^y for x,y in zip(files[a][off:off+65536],files[b][off:off+65536]))==files[p][off:off+65536]
  results.append({'case':name,'member':member,'fault_op':operation,'original_cqe':'background EIO and nonzero server exit','failed_metadata':'all6 DIRTY','retry':'full sync and all256 physical stripes passed','fixture':str(directory)})
  (out/'manifest.json').write_text(json.dumps({'scope':'external FUSE background sync/finalization faults, not performance','complete':False,'results':results},indent=2)+'\n');print('PASS',name,flush=True)
 finally:
  if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,timeout=30)
  if server is not None and server.poll() is None:server.wait(timeout=30)
  for log in logs:log.close()
  subprocess.run(['fusermount3','-u',str(mount)],capture_output=True,timeout=20)
  if fs.poll() is None:fs.wait(timeout=20)
  fslog.close()
 subprocess.run(['python3','scripts/raid5/retire_backend_fixtures.py'],check=True,capture_output=True)
j=json.loads((out/'manifest.json').read_text());j['complete']=True;(out/'manifest.json').write_text(json.dumps(j,indent=2)+'\n');print('PASS',out,flush=True)
