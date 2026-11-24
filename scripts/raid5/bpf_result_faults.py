#!/usr/bin/env python3
"""Externally alter BPF descriptors before submit; CQEs are real kernel results.
Short mode computes only512 bytes, testing target's expected4096 check. USER
mode uses an inaccessible USER source. No production hook/synthetic CQE.
"""
import hashlib,json,os,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root);binary=Path(os.environ['RAID5_BINARY']).resolve();node=Path('/dev/ublkb1100');assert not node.exists()
subprocess.run(['clang-19','-O2','-Wall','-Wextra','-Werror','scripts/raid5/command_fault.c','-o','target/raid5-command-fault'],check=True)
out=root/'docs/raid5/bpf-result-faults'/str(time.time_ns());out.mkdir(parents=True);results=[]
for scope,mode in [(scope,mode) for scope in ['normal','background','repair'] for mode in ['short','user']]:
 name=scope+'-'+mode
 directory=Path(tempfile.mkdtemp(prefix='raid5-bpf-result-',dir='/home/xiao'));paths=[directory/f'm{i}' for i in range(3)];log=(out/f'{name}.log').open('w');server=None;ok=False
 for p in paths:
  with p.open('xb') as h:h.truncate(1048576+16*65536)
 try:
  command=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8',*map(str,paths)]
  if scope!='normal':
   preparelog=(out/f'{name}-prepare.log').open('w');prepare=subprocess.Popen(command,stdout=preparelog,stderr=preparelog)
   for _ in range(1000):
    assert prepare.poll() is None
    if node.exists():break
    time.sleep(.01)
   else:raise AssertionError('prepare timeout')
   prepare.kill();prepare.wait(timeout=15);preparelog.close();subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30)
   command=command[:command.index(str(paths[0]))]+['--assemble','--sync-rate','64KiB' if scope=='repair' else '0',*map(str,paths)]
  env=dict(os.environ,RAID5_FAULT_BPF_DESCRIPTOR=mode)
  if scope=='background':env['RAID5_FAULT_BPF_BACKGROUND']='1'
  server=subprocess.Popen(['target/raid5-command-fault',str(directory/'pid'),*command],env=env,stdout=log,stderr=log)
  if scope!='background':
   for _ in range(2000):
    assert server.poll() is None,(out/f'{name}.log').read_text()
    if node.exists():break
    time.sleep(.01)
   else:raise AssertionError('startup timeout')
   if scope=='repair':subprocess.run([str(binary),'raid5-sync','-n','1100','--pause'],check=True,capture_output=True,timeout=15)
   code="import mmap,os,errno;fd=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);b=mmap.mmap(-1,4096);b[:]=bytes([165])*4096\ntry:os.pwrite(fd,b,0)\nexcept OSError as e:assert e.errno==errno.EIO;print('EXPECTED_EIO')\nelse:raise AssertionError('unexpected success')\nfinally:os.close(fd);b.close()"
   r=subprocess.run(['python3','-c',code.replace('os.pwrite(fd,b,0)','os.pwrite(fd,b,1966080)' if scope=='repair' else 'os.pwrite(fd,b,0)')],capture_output=True,text=True,timeout=30);(out/f'{name}-client.log').write_text(r.stdout+r.stderr);assert r.returncode==0 and 'EXPECTED_EIO' in r.stdout,r.stderr
  assert server.wait(timeout=30)!=0;log.close();text=(out/f'{name}.log').read_text();expected=512 if mode=='short' else -5
  assert f'OBSERVED_BPF_CQE res={expected}' in text,text;assert 'total_writes=0' in text,text
  if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,check=True,timeout=30)
  for p in paths:
   raw=p.read_bytes();assert raw[1048576:]==bytes(16*65536),'data/parity modified after rejected BPF'
   assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096]),'false CLEAN'
  # No injected retry: independently check full data/parity after dirty full sync.
  retrylog=(out/f'{name}-retry.log').open('w');server=subprocess.Popen([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8','--assemble',*map(str,paths)],stdout=retrylog,stderr=retrylog)
  for _ in range(2000):
   assert server.poll() is None
   try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
   except (ValueError,FileNotFoundError):s={}
   if s.get('pid')==server.pid and s.get('state')=='RUNNING':break
   time.sleep(.01)
  else:raise AssertionError('retry timeout')
  subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=30);assert server.wait(timeout=30)==0;retrylog.close()
  assert all(p.read_bytes()[1048576:]==bytes(16*65536) for p in paths)
  results.append(dict(scope=scope,mode=mode,cqe=expected,client='EIO' if scope!='background' else 'no frontend request',backend_write_sqes=0,dirty_slots=6,retry='all data/parity zero after full sync'));ok=True;print('PASS',mode,flush=True)
 finally:
  if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,timeout=30)
  if server is not None and server.poll() is None:server.wait(timeout=30)
  if not log.closed:log.close()
  if ok:shutil.rmtree(directory)
  (out/'manifest.json').write_text(json.dumps(dict(complete=len(results)==6,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),results=results),indent=2));print(out,flush=True)
