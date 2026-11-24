#!/usr/bin/env python3
"""Real worker bootstrap resource failure: no writable half-device publication."""
import json,os,resource,subprocess,tempfile
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
assert not Path('/dev/ublkb1100').exists()
directory=Path(tempfile.mkdtemp(prefix='raid5-bootstrap-failure-',dir='/home/xiao'));members=[directory/f'member{i}' for i in range(3)]
for path in members:
 with path.open('xb') as f:f.truncate(17*1048576)
def map_ids():
 r=subprocess.run(['/usr/sbin/bpftool','-j','map','show'],capture_output=True,text=True,check=True)
 return {m['id'] for m in json.loads(r.stdout)}
before=map_ids()
def limit():resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
r=subprocess.run([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','8','-d','8',*map(str,members)],preexec_fn=limit,capture_output=True,text=True,timeout=30)
(root/'docs/raid5/bootstrap-failure.log').write_text(r.stdout+r.stderr)
assert r.returncode!=0
assert 'bootstrap rolled back before publication' in r.stderr,r.stderr
assert not Path('/dev/ublkb1100').exists()
for member in members:
 raw=member.read_bytes()
 for off in (0,4096):assert int.from_bytes(raw[off+20:off+24],'little')==2
 assert raw[1048576:]==bytes(16*1048576),'bootstrap mutated data'
# Kernel delayed map freeing may outlive a finished worker by a few RCU ticks.
import time
for _ in range(100):
 extra=map_ids()-before
 if not extra:break
 time.sleep(.02)
assert not extra,extra
print('PASS resource-exhausted worker bootstrap: peers joined before FETCH/publication, DIRTY identity retained, no data change/map leak')
