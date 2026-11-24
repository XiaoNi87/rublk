#!/usr/bin/env python3
"""Authorized QEMU NVMe target smoke; no physical-device performance claim."""
import json,mmap,os,subprocess,time,sys
from pathlib import Path
from nvme_members import resolve_members
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
node=Path('/dev/ublkb1100');assert not node.exists()
assert json.loads((root/'docs/raid5/nvme-preparation.json').read_text())['complete']
logpath=root/'docs/raid5/nvme-target.log';log=logpath.open('w')
p=subprocess.Popen([str(binary),'add','raid5','-z','--foreground','-n','1100','-q','4','-d','64','-b','128KiB',*(['--assemble'] if '--assemble' in sys.argv else []),*map(str,resolve_members())],stdout=log,stderr=log)
try:
 for _ in range(30000):
  assert p.poll() is None,logpath.read_text()
  if node.exists():break
  time.sleep(.01)
 else:raise AssertionError('startup timeout')
 status=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text());assert status['pid']==p.pid and status['sync']['started_unix_ns']==0,status
 fd=os.open(node,os.O_RDWR|os.O_DIRECT)
 for offset,size in [(0,131072),(131072,131072),(262144,131072),(8192,4096),(61440,12288),(65536,262144),(4292870144-4096,4096)]:
  data=bytes(((i*13+17)^((i>>8)*29)^(offset>>9))&255 for i in range(size));buf=mmap.mmap(-1,size);buf[:]=data
  assert os.pwrite(fd,buf,offset)==size;os.fsync(fd)
  check=mmap.mmap(-1,size);assert os.preadv(fd,[check],offset)==size and check[:]==data
  buf.close();check.close()
 os.close(fd)
 # Independent fio content checks on 128 MiB with actual asynchronous queueing.
 r=subprocess.run(['fio','--name=raid5-nvme-correctness','--filename='+str(node),'--ioengine=io_uring','--direct=1','--iodepth=32','--numjobs=4','--cpus_allowed=0,2,4,6','--cpus_allowed_policy=split','--rw=randwrite','--bs=4k','--size=32M','--offset_increment=32M','--verify=crc32c','--do_verify=1','--verify_fatal=1','--randrepeat=1','--group_reporting=1','--output-format=json','--output='+str(root/'docs/raid5/nvme-fio-correctness.json')],capture_output=True,text=True,timeout=180)
 assert r.returncode==0,(r.stdout,r.stderr)
 raw=(root/'docs/raid5/nvme-fio-correctness.json').read_text();begin=raw.index('{');report=json.loads(raw[begin:])
 (root/'docs/raid5/nvme-fio-warnings.log').write_text(raw[:begin]+r.stdout+r.stderr)
 (root/'docs/raid5/nvme-fio-correctness.json').write_text(json.dumps(report,indent=2)+'\n')
 assert all(j['error']==0 for j in report['jobs'])
 print('PASS four-queue NVMe target, three parity rotations, RMW/cross-boundary/tail, FLUSH, fio independent CRC32C verify')
finally:
 if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,check=True,timeout=30)
 p.wait(timeout=30);log.close();assert p.returncode==0,logpath.read_text();assert not node.exists()
