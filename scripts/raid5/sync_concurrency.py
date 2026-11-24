#!/usr/bin/env python3
"""Held ORIGINAL scanner/repair backend operations versus foreground and FLUSH.
FUSE storage is an external fault fixture, never a zero-copy/performance claim.
"""
import argparse,concurrent.futures,json,os,shutil,signal,subprocess,tempfile,time
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--faults',action='store_true');args=parser.parse_args()
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root);binary=Path(os.environ['RAID5_BINARY']).resolve()
P=1048576;C=65536;N=64;CAP=N*2*C;stripe=32;off=stripe*2*C;node=Path('/dev/ublkb1100');assert not node.exists()
out=root/'docs/raid5/sync-concurrency'/str(time.time_ns());out.mkdir(parents=True);results=[]
def run(cmd):return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=30)
def wait(predicate,label,seconds=20):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  value=predicate()
  if value:return value
  time.sleep(.01)
 raise AssertionError('timeout '+label)
def status():return json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
client='''import mmap,os,sys
fd=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);b=mmap.mmap(-1,4096)
try:
 if sys.argv[1]=='write': b[:]=bytes([int(sys.argv[3])])*4096;assert os.pwrite(fd,b,int(sys.argv[2]))==4096
 elif sys.argv[1]=='read': assert os.preadv(fd,[b],int(sys.argv[2]))==4096;assert b[:]==bytes([int(sys.argv[3])])*4096
 else:os.fsync(fd)
finally:os.close(fd);b.close()
'''
cases=[(owner,op,'success') for owner in ['scanner','foreground'] for op in ['read','write']]
if args.faults:cases=[('foreground',op,action) for op in ['read','write'] for action in ['error','short']]
for owner,operation,action in cases:
  name=owner+'-'+operation+'-'+action;directory=Path(tempfile.mkdtemp(prefix='raid5-sync-order-',dir='/home/xiao'));backing=directory/'backing';backing.mkdir();mount=directory/'mount';mount.mkdir();control=directory/'control';paths=[mount/f'member{i}' for i in range(3)];logs=[];server=None;children=[];success=False
  for i in range(3):
   with (backing/f'member{i}').open('xb') as h:h.truncate(P+N*C)
  fslog=(out/f'{name}-fuse.log').open('w');fs=subprocess.Popen(['target/raid5-fault-fs',str(mount),str(backing),str(control)],env=dict(os.environ,RAID5_FAULT_FS_MULTITHREAD='1'),stdout=fslog,stderr=fslog)
  def start(assemble=False):
   h=(out/f'{name}-session-{len(logs)}.log').open('w');logs.append(h)
   cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','4','-d','16','--sync-rate','1MiB']
   if assemble:cmd+=['--assemble']
   p=subprocess.Popen(cmd+list(map(str,paths)),stdout=h,stderr=h)
   def ready():
    assert p.poll() is None
    try:s=status()
    except (FileNotFoundError,ValueError):return False
    return s if s['pid']==p.pid and s['state'] in ['RUNNING','RECOVERING'] else False
   wait(ready,'startup');return p
  def control_sync(*args):return json.loads(run([str(binary),'raid5-sync','-n','1100',*args]).stdout)
  def launch(op,offset,value=0):
   p=subprocess.Popen(['python3','-c',client,op,str(offset),str(value)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True);children.append(p);return p
  def completed(p):
   text=p.communicate(timeout=30)[0];assert p.returncode==0,text
  try:
   wait(lambda:os.path.ismount(mount),'FUSE mount');server=start();server.kill();server.wait(timeout=15);run([str(binary),'del','-n','1100'])
   # Make stale parity visible: foreground-first RMW must repair the complete
   # stripe, not silently use old parity. Data remains independently all zero.
   parity=2-stripe%3
   with (backing/f'member{parity}').open('r+b',buffering=0) as h:h.seek(P+stripe*C);h.write(bytes([0xdd])*C);os.fsync(h.fileno())
   server=start(True);s=control_sync('--pause');assert s['sync']['frontier']<stripe,s
   member=((parity+1)%3) if operation=='read' else parity
   control.write_text(f'{member} {operation} {P+stripe*C} hold-{action}\n')
   if owner=='scanner':control_sync('--rate','16MiB');control_sync('--resume')
   else:writer=launch('write',off,165)
   wait(lambda:Path(str(control)+'.hit').exists(),'held original '+name)
   held_status=status();assert held_status['sync']['frontier']<=stripe,held_status
   if owner=='scanner':writer=launch('write',off,165)
   else:control_sync('--rate','16MiB');control_sync('--resume')
   # Different stripe can finish while the selected original remains pending.
   other=launch('write',off+4*C,90);completed(other)
   reader=launch('read',off+4096,0);flush=launch('flush',0)
   time.sleep(.2);assert writer.poll() is None and reader.poll() is None and flush.poll() is None,'permit/admission escaped before original completion'
   during=status();assert during['sync']['frontier']<=stripe,during
   Path(str(control)+'.release').touch()
   if action=='success':completed(writer);completed(reader);completed(flush)
   else:
    for child in [writer,reader,flush]:
     text=child.communicate(timeout=30)[0];assert child.returncode!=0 and 'Input/output error' in text,text
    server.wait(timeout=30);assert server.returncode!=0
    if node.exists():run([str(binary),'del','-n','1100'])
    for i in range(3):
     raw=(backing/f'member{i}').read_bytes()[:8192];assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096])
    control.write_text('');server=start(True);control_sync('--rate','16MiB')
   wait(lambda:status()['state']=='RUNNING','full sync complete')
   completed(launch('read',off,165 if action=='success' else 0));completed(launch('read',off+4*C,90))
   run([str(binary),'del','-n','1100']);assert server.wait(timeout=30)==0
   files=[(backing/f'member{i}').read_bytes() for i in range(3)];expected=bytearray(CAP);expected[off:off+4096]=bytes([165 if action=='success' else 0])*4096;expected[off+4*C:off+4*C+4096]=bytes([90])*4096
   for st in range(N):
    p=2-st%3;a=(p+1)%3;b=(p+2)%3;o=P+st*C;blocks=[f[o:o+C] for f in files]
    assert blocks[a]+blocks[b]==expected[st*2*C:(st+1)*2*C],('content',st)
    assert bytes(x^y for x,y in zip(blocks[a],blocks[b]))==blocks[p],('parity',st)
   results.append(dict(case=name,held=held_status,during=during,blocked=['same-stripe writer','same-stripe reader','FLUSH'],independent_stripe_completed=True,physical_stripes=N,passed=True));success=True;print('PASS',name,flush=True)
  finally:
   Path(str(control)+'.release').touch()
   for p in children:
    if p.poll() is None:p.communicate(timeout=30)
   if node.exists():run([str(binary),'del','-n','1100'])
   if server is not None and server.poll() is None:server.wait(timeout=30)
   for h in logs:h.close()
   run(['fusermount3','-u',str(mount)]);fs.wait(timeout=20);fslog.close()
   if success:shutil.rmtree(directory)
   (out/'manifest.json').write_text(json.dumps(dict(complete=len(results)==4,results=results),indent=2));print(out,flush=True)
