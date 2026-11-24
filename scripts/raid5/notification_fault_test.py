#!/usr/bin/env python3
"""Notification fault acceptance: external ptrace + disposable FUSE file members."""
import argparse,hashlib,json,mmap,os,re,signal,subprocess,tempfile,time
parser=argparse.ArgumentParser();parser.add_argument("--negative-control",action="store_true");args=parser.parse_args()
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ['RAID5_BINARY']).resolve()
tracer=root/'target/raid5-notification-fault'
out=root/'docs/raid5/notification-faults'/str(time.time_ns());out.mkdir(parents=True)
results=[];device='1102';node=Path('/dev/ublkb'+device);sysnode=Path('/sys/class/ublk-char/ublkc'+device)
assert not sysnode.exists()
flags=subprocess.check_output(['pkg-config','--cflags','--libs','fuse3'],text=True).split()
subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/fault_fs.c','-o','target/raid5-fault-fs',*flags],check=True)
def until(check, seconds=15):
 deadline=time.monotonic()+seconds
 while True:
  value=check()
  if value:return value
  assert time.monotonic()<deadline,'condition timed out'
  time.sleep(.01)
def maps():return {m['id'] for m in json.loads(subprocess.check_output(['bpftool','-j','map','show']))}
def status(pid,state):
 try:s=json.loads(Path('/run/rublk-raid5/raid5-'+device+'.status.json').read_text())
 except (FileNotFoundError,ValueError):return False
 return s if s.get('pid')==pid and s.get('state')==state else False
for mode in (['poll'] if args.negative_control else ['poll','drain']):
 for case in (['normal-idle-fetch','sync-paused'] if args.negative_control else ['normal-idle-fetch','sync-paused','normal-inflight','sync-inflight']):
  name=mode+'-'+case;d=out/name;d.mkdir();work=Path(tempfile.mkdtemp(prefix='raid5-notify-',dir='/home/xiao'))
  backing=work/'backing';backing.mkdir();mount=work/'mount';mount.mkdir();control=work/'control';arm=work/'arm'
  for i in range(3):
   with (backing/f'member{i}').open('xb') as f:f.truncate(17*1048576)
  paths=[mount/f'member{i}' for i in range(3)];logs=[];server=None;client=None;start=None;pid=None
  before=maps();fl=(d/'fuse.log').open('w');fs=subprocess.Popen(['target/raid5-fault-fs',str(mount),str(backing),str(control)],env={**os.environ,'RAID5_FAULT_FS_MULTITHREAD':'1'},stdout=fl,stderr=fl)
  def launch(label,assemble=False,traced=False,rate='0'):
   log=(d/(label+'.log')).open('w');logs.append(log)
   cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n',device,'-q','2','-d','8','--sync-window','4','--sync-rate',rate,*(['--assemble'] if assemble else []),*map(str,paths)]
   env=os.environ.copy()
   if traced:
    cmd=[str(tracer),str(d/'child.pid'),*cmd];env.update(RAID5_FAULT_NOTIFICATION=mode,RAID5_FAULT_NOTIFICATION_ARM=str(arm))
    if case.startswith('sync'):env['RAID5_FAULT_NOTIFICATION_SYNC']='1'
   return subprocess.Popen(cmd,env=env,stdout=log,stderr=log,start_new_session=True)
  def text():return (d/'fault.log').read_text()
  def syncctl(*args):return subprocess.run([str(binary),'raid5-sync','-n',device,*args],capture_output=True,text=True,check=True,timeout=15)
  def deleted():return not sysnode.exists()
  try:
   until(lambda:os.path.ismount(mount))
   server=launch('prepare');until(lambda:status(server.pid,'RUNNING'),30)
   server.kill();server.wait(timeout=15)
   subprocess.run([str(binary),'del','-n',device],check=True,capture_output=True,timeout=15);until(deleted)
   server=launch('fault',True,True,'1MiB' if case.startswith('sync') else '0')
   pid=until(lambda:int((d/'child.pid').read_text()) if (d/'child.pid').exists() else False)
   snap=until(lambda:status(pid,'RECOVERING' if case.startswith('sync') else 'RUNNING'),30)
   until(lambda:'NOTIFICATION_SELECTED' in text())
   if case=='sync-paused':
    response=syncctl('--pause');(d/'pause.log').write_text(response.stdout+response.stderr)
    snap=until(lambda:(s if (s:=status(pid,'RECOVERING')) and s['sync'].get('paused') else False))
   if case.endswith('inflight'):
    control.write_text('0 read 17760256 hold-success\n' if case=='sync-inflight' else '2 write 1048576 hold-success\n')
    if case=='sync-inflight':syncctl('--rate','0')
    else:
     until(lambda:node.exists())
     code="import mmap,os; b=mmap.mmap(-1,4096);b[:]=bytes([165])*4096;f=os.open('/dev/ublkb1102',os.O_RDWR|os.O_DIRECT)\ntry: print('WRITE_SUCCESS',os.pwrite(f,b,0))\nexcept OSError as e: print('WRITE_ERROR',e.errno)\nfinally: os.close(f);b.close()"
     client=subprocess.Popen(['python3','-c',code],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    until(lambda:Path(str(control)+'.hit').exists(),30)
   (d/'armed-status.json').write_text(json.dumps(snap,indent=2)+'\n')
   start=time.monotonic();arm.touch()
   if args.negative_control:
    until(lambda:'NEGATIVE_NOTIFICATION_CQE' in text(),10)
    time.sleep(3)
    assert server.poll() is None and Path(f'/proc/{pid}/exe').exists(),'old service did not reproduce delayed propagation'
    assert 'RAID5 fatal notification:' not in text()
    os.kill(pid,signal.SIGKILL);server.wait(timeout=10)
    subprocess.run([str(binary),'del','-n',device],capture_output=True,check=True,timeout=15)
   else:
    until(lambda:'RAID5 fatal notification:' in text(),10)
   if case.endswith('inflight'):
    time.sleep(.2)
    assert server.poll() is None,'owner exited before original backend CQE'
    assert Path(f'/proc/{pid}/exe').exists(),'service resources released before CQE'
    if client:assert client.poll() is None,'client escaped held backend'
    (d/'pending-proof.json').write_text(json.dumps({'held_callback':True,'service_alive_after_fatal':True,'held_after_fatal_seconds':.2,'original_cqe_unavailable':True},indent=2)+'\n')
    Path(str(control)+'.release').touch()
   server.wait(timeout=20);elapsed=time.monotonic()-start
   assert server.returncode!=0,text()
   content=text();assert 'injected=1 negative_seen=1' in content,content
   if client:
    clienttext=client.communicate(timeout=10)[0];(d/'client.log').write_text(clienttext)
    assert client.returncode==0 and 'WRITE_ERROR 5' in clienttext,clienttext
   until(deleted);until(lambda:not (maps()-before))
   for i in range(3):
    raw=(backing/f'member{i}').read_bytes()[:8192];(d/f'dirty-member{i}.bin').write_bytes(raw)
    assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096]),'incorrect CLEAN'
   server=launch('retry',True);until(lambda:status(server.pid,'RUNNING'),30)
   data=mmap.mmap(-1,4096);fd=os.open(node,os.O_RDWR|os.O_DIRECT);data[:]=bytes([90])*4096
   assert os.pwrite(fd,data,4096)==4096;os.fsync(fd);data[:]=bytes(4096)
   assert os.preadv(fd,[data],4096)==4096 and data[:]==bytes([90])*4096;os.close(fd);data.close()
   subprocess.run([str(binary),'del','-n',device],check=True,capture_output=True,timeout=15)
   assert server.wait(timeout=15)==0;until(deleted);until(lambda:not (maps()-before))
   contents=[(backing/f'member{i}').read_bytes() for i in range(3)]
   for raw in contents:assert all(int.from_bytes(raw[o+20:o+24],'little')==1 for o in [0,4096])
   for stripe in range(256):
    p=2-stripe%3;a,b=(p+1)%3,(p+2)%3;off=1048576+stripe*65536
    assert bytes(x^y for x,y in zip(contents[a][off:off+65536],contents[b][off:off+65536]))==contents[p][off:off+65536]
   results.append({'case':name,'passed':True,'negative_control_expected_hang':args.negative_control,'failure_to_exit_seconds':elapsed,'exit_limit_seconds':20,'dirty_slots':6,'retry_physical_stripes':256,'retry_read_write_flush_clean':True,'no_device_or_extra_bpf_maps':True,'held_original_backend':case.endswith('inflight')})
   print('PASS',name,elapsed,flush=True)
  finally:
   Path(str(control)+'.release').touch()
   if client and client.poll() is None:client.kill();client.wait(timeout=5)
   if sysnode.exists():subprocess.run([str(binary),'del','-n',device],capture_output=True,timeout=15)
   if server and server.poll() is None:
    try:server.wait(timeout=5)
    except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL);server.wait(timeout=5)
   for log in logs:log.close()
   subprocess.run(['fusermount3','-u',str(mount)],capture_output=True,timeout=10);fs.wait(timeout=10);fl.close()
   # Only retire this owned disposable fixture after unmount.
   import shutil
   shutil.rmtree(work)
  (out/'manifest.json').write_text(json.dumps({'complete':False,'binary':str(binary),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'results':results},indent=2)+'\n')
j=json.loads((out/'manifest.json').read_text());j['complete']=True;(out/'manifest.json').write_text(json.dumps(j,indent=2)+'\n');print(out,flush=True)
