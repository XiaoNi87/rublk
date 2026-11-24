#!/usr/bin/env python3
"""Destructive preparation of the three explicitly user-authorized QEMU NVMe."""
import json,os,stat,subprocess,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
from nvme_members import resolve_members
devices=resolve_members()
keys=set();inventory=[]
for dev in devices:
 s=dev.stat();assert stat.S_ISBLK(s.st_mode)
 sys=Path('/sys/class/block')/dev.name
 descendants=[sys,*sys.glob(dev.name+'p*')]
 for entry in descendants:
  assert not list((entry/'holders').iterdir()),f'active holder {entry}'
  keys.add((entry/'dev').read_text().strip())
 inventory.append({'path':str(dev),'bytes':int((sys/'size').read_text())*512,'model':(sys/'device/model').read_text().strip(),'before_wipefs':subprocess.run(['wipefs','--json',str(dev)],capture_output=True,text=True,check=True).stdout})
 assert inventory[-1]['bytes']==2147483648
# Every namespace partition must be unmounted and absent from swap/open FDs.
for line in Path('/proc/self/mountinfo').read_text().splitlines():assert line.split()[2] not in keys,line
for line in Path('/proc/swaps').read_text().splitlines()[1:]:
 s=Path(line.split()[0]).stat();assert f'{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}' not in keys
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit():continue
 try:fds=list((proc/'fd').iterdir())
 except (FileNotFoundError,PermissionError):continue
 for fd in fds:
  try:s=fd.stat()
  except (FileNotFoundError,PermissionError):continue
  assert not (stat.S_ISBLK(s.st_mode) and f'{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}' in keys),f'open member {fd}'
assert len({x['bytes'] for x in inventory})==1
report={'authorization':'User 2026-10-27: all eight NVMe are disposable test disks','environment':'QEMU 2 GiB NVMe; functional/emulated comparison only, no physical NVMe claims','inventory':inventory,'started_unix_ns':time.time_ns(),'complete':False}
path=root/'docs/raid5/nvme-preparation.json';path.write_text(json.dumps(report,indent=2)+'\n')
for dev in devices:
 subprocess.run(['wipefs','--all',str(dev)],check=True)
 subprocess.run(['blockdev','--rereadpt',str(dev)],check=True)
# Full explicit zero writes, no assumption about discard read-after semantics.
workers=[]
for i,dev in enumerate(devices):
 log=(root/'docs/raid5'/f'nvme-zero-{i}.json').open('w')
 workers.append((subprocess.Popen(['fio','--name=authorized-zero','--filename='+str(dev),'--rw=write','--bs=1M','--size=2G','--ioengine=io_uring','--iodepth=16','--direct=1','--zero_buffers=1','--end_fsync=1','--output-format=json'],stdout=log,stderr=subprocess.PIPE,text=True),log))
for p,log in workers:
 _,error=p.communicate(timeout=180);log.close();assert p.returncode==0,error
report.update(complete=True,completed_unix_ns=time.time_ns());path.write_text(json.dumps(report,indent=2)+'\n')
print('PASS authorized members checked idle, signatures removed, all 6 GiB explicitly zeroed and flushed')
