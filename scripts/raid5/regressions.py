#!/usr/bin/env python3
"""Build-isolated existing-target regressions with positive device observations."""
import argparse,hashlib,json,os,shutil,subprocess,threading,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root)
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--prepare-only',action='store_true');p.add_argument('--run-only',action='store_true');args=p.parse_args();assert not(args.prepare_only and args.run_only)
out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
env=dict(os.environ,PATH='/home/xiao/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',CARGO_BUILD_JOBS='2',CARGO_INCREMENTAL='0');env.pop('RUSTFLAGS',None)
state_path=out/'manifest.json';state=json.loads(state_path.read_text()) if state_path.exists() else {'started_unix_ns':time.time_ns(),'complete':False,'variants':{},'runs':[],'optional_limits':['compress feature not selected in the required default/no-default/raid5 variants','vram compile checked in default configurations; no GPU device acceptance in this QEMU VM']}
def save():
 tmp=state_path.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2)+'\n');os.replace(tmp,state_path)
def run(name,cmd,timeout=900,run_env=env):
 start=time.time_ns()
 try:r=subprocess.run(cmd,env=run_env,capture_output=True,text=True,timeout=timeout)
 except subprocess.TimeoutExpired as e:
  state['runs'].append({'name':name,'command':cmd,'start_unix_ns':start,'finish_unix_ns':time.time_ns(),'exit':'timeout'});save()
  (out/(name+'.log')).write_text(str(e));raise
 (out/(name+'.log')).write_text(r.stdout+r.stderr);state['runs'].append({'name':name,'command':cmd,'start_unix_ns':start,'finish_unix_ns':time.time_ns(),'exit':r.returncode});save();assert r.returncode==0,(name,r.stderr[-3000:],r.stdout[-3000:]);return r.stdout
variants={'default-raid5':['--features','raid5'],'default':[],'minimal':['--no-default-features'],'minimal-raid5':['--no-default-features','--features','raid5']}
if not args.run_only:
 state['rustc']=run('rustc',['rustc','-vV'])
 for name,flags in variants.items():
  text=run(name+'-build',['cargo','test','--release',*flags,'--test','basic','--no-run','--message-format=json'])
  artifacts=[]
  for line in text.splitlines():
   try:j=json.loads(line)
   except ValueError:continue
   if j.get('reason')=='compiler-artifact' and j.get('executable') and j['target']['name']=='basic':artifacts.append(j['executable'])
  assert len(artifacts)==1,artifacts
  directory=out/name;directory.mkdir(exist_ok=True)
  binaries={}
  for label,path in [('basic',Path(artifacts[0])),('rublk',root/'target/release/rublk')]:
   destination=directory/label;shutil.copy2(path,destination);binaries[label]={'path':str(destination),'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}
  sections=run(name+'-elf',['readelf','--wide','--sections',binaries['rublk']['path']]);assert '__llvm_prf' not in sections and '__llvm_cov' not in sections
  state['variants'][name]={'flags':flags,'binaries':binaries,'complete':False};save()
if args.prepare_only:print('PREPARED',out,flush=True);raise SystemExit(0)
assert Path('/dev/ublk-control').exists();assert not list(Path('/dev').glob('ublkb*')),'run exclusively after RAID5 device suites'
tools=Path('/tmp/raid5-step17-tools/extracted');test_env=dict(env,PATH=str(tools/'usr/bin')+':'+str(tools/'usr/sbin')+':'+env['PATH'],LD_LIBRARY_PATH=str(tools/'usr/lib/aarch64-linux-gnu'),TMPDIR='/tmp')
root_prefix=['sudo','-n','env',*[key+'='+test_env[key] for key in ['PATH','LD_LIBRARY_PATH','TMPDIR']]]
for name,command in [('qemu-img',['qemu-img','--version']),('btrfs',['mkfs.btrfs','--version']),('ext4',['mkfs.ext4','-V']),('discard',['blkdiscard','--version'])]:run(name+'-prerequisite',root_prefix+command)
excluded=[]
if '\tbtrfs\n' not in Path('/proc/filesystems').read_text():
 kernel=subprocess.run(['uname','-r'],capture_output=True,text=True,check=True).stdout.strip();config=Path('/boot')/('config-'+kernel)
 assert config.exists() and '# CONFIG_BTRFS_FS is not set' in config.read_text(),'investigate/load configured btrfs instead of skipping'
 excluded=['integration::test_ublk_format_mount_zoned'];state['explicit_skips']=[{'test':excluded[0],'reason':'running kernel CONFIG_BTRFS_FS is not set; mkfs tool exists but filesystem mount cannot run','kernel':kernel,'config_path':str(config)}];save()
for name,entry in state['variants'].items():
 binary=entry['binaries']['rublk'];test=entry['binaries']['basic']
 for artifact in entry['binaries'].values():assert hashlib.sha256(Path(artifact['path']).read_bytes()).hexdigest()==artifact['sha256']
 features=run(name+'-features',root_prefix+[binary['path'],'features']);assert 'ZONED' in features and 'USER_COPY' in features
 done=threading.Event();observed=[];seen=set()
 def observe():
  while not done.wait(.1):
   for node in Path('/sys/class/ublk-char').glob('ublkc*'):
    key=node.name
    result=subprocess.run(root_prefix+[binary['path'],'list','-n',key[5:]],capture_output=True,text=True,timeout=5)
    text=result.stdout
    identity=(key,text)
    if result.returncode==0 and 'state LIVE' in text and identity not in seen:
     seen.add(identity);observed.append({'unix_ns':time.time_ns(),'node':key,'output':text})
 thread=threading.Thread(target=observe,daemon=True);thread.start()
 try:output=run(name+'-tests',root_prefix+[test['path'],'--test-threads=1','--nocapture',*[arg for name in excluded for arg in ['--skip',name]]],timeout=900)
 finally:
  done.set();thread.join(timeout=10);assert not thread.is_alive();(out/(name+'-devices.json')).write_text(json.dumps(observed,indent=2)+'\n')
 assert 'test result: ok.' in output and '0 failed' in output
 positive={target for target in ['null','loop','qcow2','zoned'] if any('"name":"'+target+'"' in row['output'] or '"name": "'+target+'"' in row['output'] for row in observed)}
 assert positive=={'null','loop','qcow2','zoned'},positive
 for _ in range(200):
  if not list(Path('/dev').glob('ublkb*')):break
  time.sleep(.05)
 assert not list(Path('/dev').glob('ublkb*')),'regression left devices behind'
 entry.update(complete=True,observed_targets=sorted(positive),observations=len(observed),explicit_skips=excluded);save();print('PASS',name,sorted(positive),flush=True)
state.update(complete=True,finished_unix_ns=time.time_ns(),runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest());save();print('PASS regressions',out,flush=True)
