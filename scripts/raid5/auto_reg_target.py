#!/usr/bin/env python3
import json,os,re,shlex,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/release/rublk'))).resolve();node=Path('/dev/ublkb1100');assert not node.exists()
directory=Path(tempfile.mkdtemp(prefix='raid5-auto-reg-',dir='/home/xiao'));members=[directory/f'member{i}' for i in range(3)]
for path in members:
 with path.open('xb') as f:f.truncate(17*1048576)
subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/occupy_buffers.c','-o','target/raid5-occupy-buffers'],check=True)
logpath=root/'docs/raid5/auto-reg-target.log';log=logpath.open('w');server=subprocess.Popen([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8',*map(str,members)],stdout=log,stderr=log);helper=None;release=directory/'release'
try:
 for _ in range(1000):
  assert server.poll() is None,logpath.read_text()
  if node.exists():break
  time.sleep(.01)
 else:raise AssertionError('startup timed out')
 time.sleep(.2);rings=[]
 for path in Path(f'/proc/{server.pid}/fdinfo').iterdir():
  text=path.read_text();found=re.search(r'SqMask:\s+(0x[0-9a-f]+)',text)
  if found and int(found.group(1),16)+1==128:rings.append(path.name)
 assert len(rings)==2,rings
 helper=subprocess.Popen(['target/raid5-occupy-buffers',str(server.pid),'8',str(release),*rings],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 assert helper.stdout.readline().strip()=='HELD',helper.communicate(timeout=10)
 trace=subprocess.run(['bpftrace','scripts/raid5/kfixed_trace.bt','-c',shlex.join(['python3','scripts/raid5/auto_reg_client.py',str(node),str(members[0]),str(release)])],capture_output=True,text=True,timeout=45)
 (root/'docs/raid5/auto-reg-target-trace.log').write_text(trace.stdout+trace.stderr);assert trace.returncode==0,(trace.stdout,trace.stderr)
 counts={k:int(v) for k,v in re.findall(r'@(bpf|copy|regfail|regok|user|kfixed|destroy): (\d+)',trace.stdout)}
 assert counts['regfail']>=1 and counts['copy']==0 and counts['bpf']>=1 and counts['regok']>=1,counts
 helper.wait(timeout=10);assert helper.returncode==0,helper.stderr.read()
 print('PASS actual target auto-registration failure without fallback/event assumption',counts)
finally:
 release.touch(exist_ok=True)
 if helper is not None:
  try:helper.wait(timeout=15)
  except subprocess.TimeoutExpired:helper.kill();helper.wait(timeout=10)
 if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,check=True,timeout=20)
 server.wait(timeout=20);log.close();assert not node.exists();assert server.returncode==0,logpath.read_text()
