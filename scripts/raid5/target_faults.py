#!/usr/bin/env python3
"""Real backend write failure without a production fault-injection switch."""
import errno,json,mmap,os,resource,signal,subprocess,tempfile,time,sys
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
node=Path('/dev/ublkb1100');assert not node.exists()
directory=Path(tempfile.mkdtemp(prefix='raid5-failure-',dir='/home/xiao'))
members=[directory/f'member{i}' for i in range(3)]
for path in members:
 with path.open('xb') as f:f.truncate(17*1048576)
status_path=Path('/run/rublk-raid5/raid5-1100.status.json')
logpath=root/'docs/raid5'/('target-broken-stderr.log' if '--broken-stderr' in sys.argv else 'target-short-read.log' if '--short-read' in sys.argv else 'target-short-rmw.log' if '--short-rmw' in sys.argv else 'target-backend-failure.log')
# The ignored signal survives exec; RLIMIT_FSIZE then makes actual filesystem
# writes return EFBIG rather than terminating the transaction owner mid-CQE.
short_read='--short-read' in sys.argv
short_rmw='--short-rmw' in sys.argv
broken_stderr='--broken-stderr' in sys.argv
full=open('/dev/full','w') if broken_stderr else None
old=signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
p=subprocess.Popen([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8',*map(str,members)],stdout=subprocess.PIPE,stderr=full if broken_stderr else subprocess.STDOUT,text=True,restore_signals=False)
signal.signal(signal.SIGXFSZ,old)
try:
 for _ in range(1000):
  assert p.poll() is None,logpath.read_text()
  if node.exists() and status_path.exists():
   s=json.loads(status_path.read_text())
   if s['pid']==p.pid and s['state']=='RUNNING':break
  time.sleep(.01)
 else:raise AssertionError('startup timed out')
 # All metadata is already durably DIRTY. No later target backend write can
 # succeed, while kernel ublk fetch/commit operations are unaffected.
 if short_read or short_rmw:os.truncate(members[0],1048576)
 else:resource.prlimit(p.pid,resource.RLIMIT_FSIZE,(0,0))
 fd=os.open(node,os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,4096);buf[:]=bytes([37])*4096
 try:
  if short_read:os.preadv(fd,[buf],0)
  else:os.pwrite(fd,buf,0)
 except OSError as e:assert e.errno==errno.EIO,e
 else:raise AssertionError('backend write error reported success')
 finally:os.close(fd);buf.close()
 output=p.communicate(timeout=20)[0];logpath.write_text(output);assert p.returncode!=0,output
 # RLIMIT also prevents optional status-file publication. Do not mistake the
 # last pre-failure RUNNING snapshot for authoritative device state.
 for member in members:
  raw=member.read_bytes()[:8192]
  for off in (0,4096):assert int.from_bytes(raw[off+20:off+24],'little')==2,'failure incorrectly CLEAN'
 if not broken_stderr:
  phase='data read: CQE 0' if short_read else 'RMW old data read: CQE 0' if short_rmw else 'RMW data write: CQE -27'
  assert phase in logpath.read_text(),logpath.read_text()
 print('broken_stderr=',broken_stderr,'short_read=',short_read,'short_rmw=',short_rmw)
 print('PASS actual backend fault -> client EIO, joined fail-stop, all six slots DIRTY')
finally:
 if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,timeout=20)
 if p.poll() is None:p.kill();p.wait(timeout=15)
 if short_read or short_rmw:os.truncate(members[0],17*1048576)
 assert not node.exists()
 (root/'docs/raid5/target-failure-fixture.json').write_text(json.dumps({'directory':str(directory),'members':list(map(str,members)),'server_exit':p.returncode},indent=2)+'\n')
