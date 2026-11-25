#!/usr/bin/env python3
"""SIGKILL with an original backend read/write held outside the target.
Cache-preserved process crashes, NOT power failure. Probe takeover before old
I/O release; then drain/delete and full-sync retry. Unflushed failed writes
have unspecified content, but parity must be repaired from surviving data.
"""
import json,os,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root);binary=Path(os.environ['RAID5_BINARY']).resolve();node=Path('/dev/ublkb1100');assert not node.exists() and not Path('/dev/ublkb1104').exists()
out=root/'docs/raid5/crash-inflight'/str(time.time_ns());out.mkdir(parents=True);results=[];P=1048576;C=65536;N=64
client_code="import mmap,os;fd=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);b=mmap.mmap(-1,4096);b[:]=bytes([165])*4096\ntry:os.pwrite(fd,b,int(__import__('sys').argv[1]))\nexcept OSError as e:assert e.errno==5;print('EIO')\nelse:raise AssertionError('crashed request succeeded')\nfinally:os.close(fd);b.close()"
def run(cmd):return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=30)
def wait(f,label,seconds=20):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  if f():return
  time.sleep(.01)
 raise AssertionError('timeout '+label)
def status():return json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
for owner in ['normal','repair','scanner']:
 for operation in ['read','write']:
  name=owner+'-'+operation;directory=Path(tempfile.mkdtemp(prefix='raid5-crash-held-',dir='/home/xiao'));back=directory/'back';back.mkdir();mount=directory/'mount';mount.mkdir();control=directory/'control';paths=[mount/f'member{i}' for i in range(3)];logs=[];server=None;client=None;ok=False
  for i in range(3):
   with (back/f'member{i}').open('xb') as h:h.truncate(P+N*C)
  fslog=(out/f'{name}-fuse.log').open('w');fs=subprocess.Popen(['target/raid5-fault-fs',str(mount),str(back),str(control)],env=dict(os.environ,RAID5_FAULT_FS_MULTITHREAD='1'),stdout=fslog,stderr=fslog)
  def start(assemble=False,rate='0'):
   h=(out/f'{name}-session-{len(logs)}.log').open('w');logs.append(h)
   cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8','--sync-rate',rate]
   if assemble:cmd+=['--assemble']
   p=subprocess.Popen(cmd+list(map(str,paths)),stdout=h,stderr=h)
   def ready():
    assert p.poll() is None
    try:s=status()
    except (FileNotFoundError,ValueError):return False
    return s['pid']==p.pid and s['state'] in ['RUNNING','RECOVERING']
   wait(ready,'startup');return p
  def ctl(*args):return json.loads(run([str(binary),'raid5-sync','-n','1100',*args]).stdout)
  try:
   wait(lambda:os.path.ismount(mount),'mount');server=start()
   if owner!='normal':
    server.kill();server.wait(timeout=15);run([str(binary),'del','-n','1100']);server=start(True,'1MiB');assert ctl('--pause')['sync']['frontier']<32
   stripe=0 if owner=='normal' else 32;parity=2-stripe%3;member=(parity+1)%3 if operation=='read' else (0 if owner=='normal' else parity)
   control.write_text(f'{member} {operation} {P+stripe*C} hold-success\n')
   if owner=='scanner':ctl('--rate','16MiB');ctl('--resume')
   else:client=subprocess.Popen(['python3','-c',client_code,str(stripe*2*C)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
   wait(lambda:Path(str(control)+'.hit').exists(),'held original')
   os.kill(server.pid,9)
   # This call uses another ublk ID, so only backend ownership/fencing can
   # reject it. The original FUSE callback still has not been released.
   probe=subprocess.run([str(binary),'add','raid5','-z','--test-files','--foreground','--assemble','-n','1104','-q','1','-d','1',*map(str,paths)],capture_output=True,text=True,timeout=5)
   (out/f'{name}-premature-takeover.log').write_text(probe.stdout+probe.stderr)
   assert probe.returncode!=0 and 'member is still owned' in (probe.stdout+probe.stderr),probe.stdout+probe.stderr
   Path(str(control)+'.release').touch();server.wait(timeout=30)
   if client is not None:
    text=client.communicate(timeout=30)[0];assert client.returncode==0 and 'EIO' in text,text
   if node.exists():run([str(binary),'del','-n','1100'])
   for i in range(3):
    raw=(back/f'member{i}').read_bytes()[:8192];assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096])
   control.write_text('');server=start(True);wait(lambda:status()['state']=='RUNNING','retry sync');run([str(binary),'del','-n','1100']);assert server.wait(timeout=30)==0
   files=[(back/f'member{i}').read_bytes() for i in range(3)]
   for st in range(N):
    p=2-st%3;a=(p+1)%3;b=(p+2)%3;o=P+st*C
    assert bytes(x^y for x,y in zip(files[a][o:o+C],files[b][o:o+C]))==files[p][o:o+C],(name,st)
   results.append(dict(case=name,premature_takeover='ownership lock rejected while old original pending',client='EIO' if client else 'no frontend request',failed_slots='all6 DIRTY',retry_physical_parity_stripes=N));ok=True;print('PASS',name,flush=True)
  finally:
   Path(str(control)+'.release').touch()
   if client is not None and client.poll() is None:client.communicate(timeout=30)
   if node.exists():run([str(binary),'del','-n','1100'])
   if server is not None and server.poll() is None:server.wait(timeout=30)
   for h in logs:h.close()
   run(['fusermount3','-u',str(mount)]);fs.wait(timeout=20);fslog.close()
   if ok:shutil.rmtree(directory)
   (out/'manifest.json').write_text(json.dumps(dict(complete=len(results)==6,results=results,scope='process-only retained-cache crash, no powercut'),indent=2));print(out,flush=True)
