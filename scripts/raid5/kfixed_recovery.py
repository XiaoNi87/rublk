#!/usr/bin/env python3
"""Real BPF/auto-reg worker fencing and no-reissue request attribution, no RAID5 parity claim."""
import json,os,signal,subprocess,time
from pathlib import Path
assert os.geteuid()==0,'run as root; never skip'
root=Path(__file__).resolve().parents[2];os.chdir(root)
fixture=json.loads(Path('docs/raid5/fixtures.json').read_text())['members'][1]
node=Path('/dev/ublkb1102');assert not node.exists()
processes=[];logs=[]
def inspect(*extra):
 r=subprocess.run(['target/debug/examples/raid5_recovery_probe','1102',*extra],capture_output=True,text=True,check=True);return json.loads(r.stdout)
def start(recover=False):
 env=os.environ.copy();env['RAID5_SPIKE_QUEUES']='2';env['RAID5_SPIKE_SYNC']='1'
 if not recover:env['RAID5_SPIKE_HOLD_OFFSET']='8388608'
 if recover:env['RAID5_SPIKE_RECOVER']='1'
 pth=Path('docs/raid5/kfixed-recovered.log' if recover else 'docs/raid5/kfixed-before-crash.log');log=pth.open('w');logs.append(log)
 p=subprocess.Popen([str(root/'target/debug/examples/raid5_kfixed_probe'),'1102',fixture],env=env,stdout=log,stderr=log);processes.append(p)
 for _ in range(200):
  if 'READY 1102' in pth.read_text():break
  assert p.poll() is None,pth.read_text();time.sleep(.025)
 assert 'READY 1102' in pth.read_text(),pth.read_text();return p
try:
 p=start();d=inspect();assert d['pid']==p.pid;assert not d['reissue'];print('initial',d,flush=True)
 subprocess.run(['python3','scripts/raid5/kfixed_client.py',str(node),fixture,'--offset','12582912'],check=True,timeout=20)
 client=subprocess.Popen(['python3','-c',"import os,mmap,errno; f=os.open('/dev/ublkb1102',os.O_RDWR|os.O_DIRECT); b=mmap.mmap(-1,8192); b[:]=bytes([136])*8192; print('SUBMIT id=1',flush=True);\ntry: os.pwrite(f,b,8388608)\nexcept OSError as e: assert e.errno==errno.EIO; print('COMPLETE id=1 EIO',flush=True)\nelse: raise AssertionError('request unexpectedly succeeded')"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
 assert client.stdout.readline().strip()=='SUBMIT id=1'
 for _ in range(200):
  if 'HOLD fetched' in Path('docs/raid5/kfixed-before-crash.log').read_text():break
  time.sleep(.025)
 assert 'HOLD fetched' in Path('docs/raid5/kfixed-before-crash.log').read_text()
 os.kill(p.pid,signal.SIGSTOP)
 for _ in range(100):
  if Path(f'/proc/{p.pid}/status').read_text().split('State:')[1].lstrip().startswith('T'):break
  time.sleep(.01)
 print('stopped worker rejected',inspect('check-fenced'),flush=True)
 # One attributed request, positively acknowledged fetched before stopping worker.
 assert client.poll() is None
 os.kill(p.pid,signal.SIGKILL);p.wait(timeout=10)
 output=client.communicate(timeout=15)[0];assert client.returncode==0,output;assert output.count('COMPLETE id=1 EIO')==1;print(output,flush=True)
 for _ in range(200):
  d=inspect()
  if d['state']==2:break
  time.sleep(.025)
 assert d['state']==2,d
 new=start(True);d=inspect();assert d['pid']==new.pid and new.pid!=p.pid;print('recovered',d,flush=True)
 # The failed request must not be replayed by the new queues.
 with open(fixture,'rb',buffering=0) as f:f.seek(8388608);assert f.read(8192)==bytes(8192),'failed request replayed'
 key=(12582912>>9)%251;expected=bytes(((i*13+17)^((i>>8)*29)^((i>>12)*71)^key)&255 for i in range(8192))
 fd=os.open(node,os.O_RDONLY);assert os.pread(fd,8192,12582912)==expected;os.close(fd)
 # Exercise normal and sync queues before orderly stop.
 subprocess.run(['taskset','-c',str(max(os.sched_getaffinity(0))),'python3','scripts/raid5/kfixed_client.py',str(node),fixture,'--offset','262144','--repeat','20'],check=True,timeout=30)
 print('PASS: BPF worker fenced; one submitted request gets one EIO, no reissue/backing modification; fresh rings/attachments and flushed epoch survive',flush=True)
finally:
 subprocess.run(['timeout','10','target/debug/rublk','del','-n','1102'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 for p in processes:
  try:p.wait(timeout=15)
  except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
 if len(processes)==2:
  assert processes[1].returncode==0,Path('docs/raid5/kfixed-recovered.log').read_text()
  text=Path('docs/raid5/kfixed-recovered.log').read_text()
  assert 'DRAINED qid=0' in text and 'DRAINED qid=1' in text and 'SYNC_DRAINED' in text,text
 for log in logs:log.close()
 assert not node.exists(),'test device cleanup failed'
