#!/usr/bin/env python3
"""Reproducible pre-target gates. Never skips devices, capabilities or failures."""
import hashlib,json,os,re,subprocess,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root)
assert Path('/dev/ublk-control').exists(),'no ublk control; no skip'
assert subprocess.check_output(['uname','-r'],text=True).strip()=='7.3.0-rc2-raid5-7.4+'
subprocess.run(['sudo','-n','true'],check=True)
for i in (1100,1101,1102):assert not Path(f'/dev/ublkb{i}').exists(),f'test ID {i} occupied'
env=os.environ.copy();env.update(CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',CARGO_INCREMENTAL='0',CARGO_TERM_COLOR='never')
def source_stamp():
 result={}
 for p in root.rglob('*'):
  rel=p.relative_to(root)
  if any(part in ('.git','target','docs') for part in rel.parts) or not p.is_file():continue
  if p.suffix in ('.rs','.c','.h','.toml','.lock','.py','.bt'):result[str(rel)]=hashlib.sha256(p.read_bytes()).hexdigest()
 return result
def maps():return {m['id'] for m in json.loads(subprocess.check_output(['sudo','-n','/usr/sbin/bpftool','-j','map','show'],text=True))}
stamp=source_stamp();baseline_maps=maps();results=[]
manifest={'kernel':subprocess.check_output(['uname','-a'],text=True).strip(),'branch':subprocess.check_output(['git','branch','--show-current'],text=True).strip(),'base':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'source_sha256':stamp,'results':results}
manifest_path=root/'docs/raid5/spike-suite.json'
def run(name,cmd,expected=0,limit=90):
 started=time.monotonic();r=subprocess.run(cmd,capture_output=True,text=True,env=env,timeout=limit)
 (root/'docs/raid5'/f'suite-{name}.log').write_text(r.stdout+r.stderr)
 results.append({'name':name,'exit':r.returncode,'expected':expected,'elapsed_seconds':time.monotonic()-started});manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
 print(name,r.returncode,flush=True);assert r.returncode==expected,(name,(r.stdout+r.stderr)[-5000:]);return r
run('build',['cargo','build','--no-default-features','--features','raid5','--bins','--examples'])
run('models',['cargo','test','--no-default-features','--features','raid5','--lib','--test','raid5_abi','--test','raid5_coordination'])
for name in ['bpf','xor','arena','file_fence','adoption']:
 run(name,['sudo','-n',f'target/debug/examples/raid5_{name}_probe'])
for name,args in [('copy-control',['--copy-control']),('kfixed',[]),('reg-failure',['--failure'])]:
 run(name,['python3','scripts/raid5/run_kfixed.py',*args])
for attempt in range(2):run(f'multiqueue-{attempt}',['python3','scripts/raid5/multiqueue_spike.py'])
fixture=json.loads(Path('docs/raid5/fixtures.json').read_text())['members'][0]
try:
 failed=run('second-attach-failure',['sudo','-n','env','RAID5_SPIKE_QUEUES=2','RAID5_SPIKE_FAIL_SECOND_ATTACH=1',str(root/'target/debug/examples/raid5_kfixed_probe'),'1101',fixture],expected=1)
 assert 'Bad file descriptor' in failed.stderr
finally:
 subprocess.run(['sudo','-n','target/debug/rublk','del','-n','1101'],capture_output=True,text=True,timeout=15)
for attempt in range(2):run(f'recovery-{attempt}',['sudo','-n','python3','scripts/raid5/kfixed_recovery.py'])
build=run('regression-build',['cargo','test','--no-default-features','--features','raid5','--test','basic','--no-run','--message-format=json'])
artifacts=[json.loads(line) for line in build.stdout.splitlines()]
binary=next(v['executable'] for v in artifacts if v.get('reason')=='compiler-artifact' and v.get('executable') and v.get('target',{}).get('name')=='basic')
for name in ['test_ublk_add_del_null','test_ublk_add_del_loop']:
 r=run(name,['sudo','-n',binary,'--exact','integration::'+name,'--nocapture','--test-threads=1'])
 assert '1 passed' in r.stdout and 'running 1 test' in r.stdout
for i in (1100,1101,1102):assert not Path(f'/dev/ublkb{i}').exists()
for _ in range(100):
 leaked=maps()-baseline_maps
 if not leaked:break
 time.sleep(.05)
assert not leaked,leaked
assert source_stamp()==stamp,'sources changed during evidence run'
manifest['complete']=True;manifest['no_new_maps_survive']=True;manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
print('PASS: all pre-target spike gates; no actual RAID5 recovery/performance acceptance implied',flush=True)
