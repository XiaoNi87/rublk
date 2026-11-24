#!/usr/bin/env python3
"""Actual CLI admission refusals and identity-preserving create-resume."""
import hashlib,json,os,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
node=Path('/dev/ublkb1100');assert not node.exists()
directory=Path(tempfile.mkdtemp(prefix='raid5-admission-',dir='/home/xiao'))
paths=[directory/f'member{i}' for i in range(3)]
for p in paths:
 with p.open('xb') as f:f.truncate(17*1048576)
base=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8']
records=[]
def hashes():return [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
def reject(name,extra=(),members=None,command=None):
 before=hashes();r=subprocess.run(command or base+list(extra)+list(map(str,members or paths)),capture_output=True,text=True,timeout=30)
 assert r.returncode!=0,(name,r.stdout,r.stderr);assert not node.exists(),name;assert hashes()==before,name
 records.append({'case':name,'exit':r.returncode,'stdout':r.stdout,'stderr':r.stderr});print('PASS reject unchanged',name,flush=True)
reject('missing-required-zero-copy',command=[x for x in base if x!='-z']+list(map(str,paths)))
reject('production-regular-file',command=[x for x in base if x!='--test-files']+list(map(str,paths)))
reject('duplicate-member',members=[paths[0],paths[0],paths[2]])
for name,extra in [('memory-limit',['--scratch-memory-limit','1KiB']),('bad-shards',['--stripe-shards','3']),('zero-sync-window',['--sync-window','0']),('too-large-sync-window',['--sync-window','257']),('empty-assemble',['--assemble'])]:reject(name,extra)
# Wrong initial parity must fail read-only, before any metadata is written.
with paths[2].open('r+b',buffering=0) as f:f.seek(1048576);f.write(bytes([71])*65536);os.fsync(f.fileno())
reject('initial-parity-inconsistent')
# Construct a partial-create state with original valid identities from a proven
# fixture, but missing one seal. This is format/integration testing, not a claim
# of a physical power-cut experiment.
source=json.loads((root/'docs/raid5/target-integration-fixture.json').read_text())['members']
for a,b in zip(source,paths):shutil.copyfile(a,b)
raw=paths[0].read_bytes()[:4096];array=raw[24:40];uuid=f'{array.hex()[:8]}-{array.hex()[8:12]}-{array.hex()[12:16]}-{array.hex()[16:20]}-{array.hex()[20:]}'
with paths[0].open('r+b',buffering=0) as f:f.seek(8192);f.write(bytes(4096));os.fsync(f.fileno())
reject('incomplete-create-normal-assembly',['--assemble'])
reject('foreign-resume-uuid',['--create-resume','00000000-0000-0000-0000-000000000001'])
reject('member-order',['--assemble'],members=[paths[1],paths[0],paths[2]])
before=[hashlib.sha256(p.read_bytes()[1048576:]).hexdigest() for p in paths]
logpath=root/'docs/raid5/create-resume.log';log=logpath.open('w');server=subprocess.Popen(base+['--create-resume',uuid]+list(map(str,paths)),stdout=log,stderr=log)
try:
 for _ in range(1000):
  assert server.poll() is None,logpath.read_text()
  if node.exists():break
  time.sleep(.01)
 else:raise AssertionError('resume timed out')
 s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text());assert s['uuid']==uuid,s
 subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,check=True,timeout=20);server.wait(timeout=20);assert server.returncode==0,logpath.read_text()
 assert [hashlib.sha256(p.read_bytes()[1048576:]).hexdigest() for p in paths]==before,'resume changed data region'
 for p in paths:
  raw=p.read_bytes();assert raw[8192:12288]!=bytes(4096)
  assert int.from_bytes(raw[20:24],'little')==1 and int.from_bytes(raw[4116:4120],'little')==1
 print('PASS actual create-resume preserves original UUID, validates parity read-only, repairs seal, stops CLEAN',flush=True)
finally:
 if node.exists():subprocess.run([str(binary),'del','-n','1100'],capture_output=True,text=True,timeout=20)
 if server.poll() is None:server.kill();server.wait(timeout=15)
 log.close();(root/'docs/raid5/admission-results.json').write_text(json.dumps({'fixture':str(directory),'results':records},indent=2)+'\n');assert not node.exists()
