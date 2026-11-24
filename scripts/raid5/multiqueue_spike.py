#!/usr/bin/env python3
"""Two real queues plus an independent USER/BPF arena worker; not RAID5 sync."""
import concurrent.futures,json,os,re,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parents[2]
fixture=json.loads((root/'docs/raid5/fixtures.json').read_text())['members'][0]
def map_ids():
 r=subprocess.run(['sudo','-n','/usr/sbin/bpftool','-j','map','show'],capture_output=True,text=True,check=True)
 return {m['id'] for m in json.loads(r.stdout)}
before_maps=map_ids()
node=Path('/dev/ublkb1101');assert not node.exists()
logpath=root/'docs/raid5/multiqueue-spike.log'
cpus=sorted(os.sched_getaffinity(0));assert len(cpus)>1
with logpath.open('w') as log:
 server=subprocess.Popen(['sudo','-n','env','RAID5_SPIKE_QUEUES=2','RAID5_SPIKE_SYNC=1',str(root/'target/debug/examples/raid5_kfixed_probe'),'1101',fixture],stdout=log,stderr=log)
 try:
  for _ in range(200):
   if 'READY 1101' in logpath.read_text():break
   assert server.poll() is None,logpath.read_text()
   time.sleep(.05)
  assert node.exists(),logpath.read_text()
  def client(i):
   for _ in range(1):
    subprocess.run(['sudo','-n','taskset','-c',str([cpus[0],cpus[-1]][i]),'python3',str(root/'scripts/raid5/kfixed_client.py'),str(node),fixture,'--offset',str([65536,262144][i]),'--size','131072','--repeat','20'],check=True,timeout=90,stdout=subprocess.DEVNULL)
  with concurrent.futures.ThreadPoolExecutor(2) as pool:list(pool.map(client,range(2)))
 finally:
  cleanup=subprocess.run(['sudo','-n','timeout','10',str(root/'target/debug/rublk'),'del','-n','1101'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
  if cleanup.returncode:
   subprocess.run(['sudo','-n','pkill','-KILL','-f','^'+re.escape(str(root/'target/debug/examples/raid5_kfixed_probe'))+' 1101 '])
  server.wait(timeout=15)
  assert cleanup.returncode==0,cleanup.stdout
text=logpath.read_text();print(text)
assert server.returncode==0 and not node.exists()
for qid in (0,1):
 assert f'DRAINED qid={qid}' in text
 assert re.search(rf'PASS: qid={qid}.*independent parity verified',text),f'queue {qid} had no write'
rows=re.findall(r'QUEUE qid=(\d+) ring_fd=(\d+) arena=(\S+) arena_map=(\d+)',text)
assert len(rows)==2,rows
for column in (1,2,3):assert len({r[column] for r in rows})==2,rows
assert re.search(r'SYNC_DRAINED rounds=\d+ overlapping_io_rounds=[1-9]\d*',text)
print('PASS: two queue rings/arenas and same tag isolated; independent sync worker overlapped and drained')

for _ in range(100):
 leaked=map_ids()-before_maps
 if not leaked:break
 time.sleep(.05)
assert not leaked, f'BPF maps survived teardown: {leaked}'
print('PASS: no new BPF map survives teardown; round overlap includes BPF object setup, not simultaneous-SQE timing proof')
