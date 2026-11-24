#!/usr/bin/env python3
"""Separate instrumented real-worker collection; archive each old collection."""
import difflib,gzip,hashlib,json,os,shutil,subprocess,tarfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root)
report_only=os.environ.get('RAID5_COVERAGE_REPORT_ONLY')=='1'
out=Path(os.environ.get('RAID5_COVERAGE_OUTPUT',str(root/'docs/raid5/coverage')))
out=out if report_only else out.resolve()
assert not list(Path('/dev').glob('ublkb110[012]')),'reserved devices active'
if not report_only and (out/'manifest.json').exists():
 previous=out.with_name('coverage-before-'+str(time.time_ns()));shutil.move(out,previous)
 shutil.make_archive(str(previous),'gztar',root_dir=previous);shutil.rmtree(previous)
out.mkdir(exist_ok=True);raw=out/'raw';raw.mkdir(exist_ok=True)
assert shutil.disk_usage(out).free>512*1048576,'less than 512MiB evidence space; relocate/archive coverage before collecting'
assert report_only or not list(raw.iterdir()),'unfinished collection: inspect/archive explicitly before retry'
build_target=Path(os.environ.get('RAID5_COVERAGE_TARGET',str(root/'target'))).resolve()
service_binary=build_target/'debug/rublk'
env=os.environ.copy();env.update(CARGO_TARGET_DIR=str(build_target),RUSTFLAGS='-C instrument-coverage',LLVM_PROFILE_FILE=str(raw/'%p-%m.profraw'),CARGO_PROFILE_DEV_DEBUG='0',CARGO_PROFILE_TEST_DEBUG='0',CARGO_INCREMENTAL='0')
def save(path,text):
 tmp=path.with_name(path.name+'.tmp');tmp.write_text(text);os.replace(tmp,path)
runs=json.loads((out/'runs.json').read_text()) if report_only else []
def run(name,cmd,timeout=180):
 profile_dir=raw/name;profile_dir.mkdir(exist_ok=True)
 pattern=str(profile_dir/'%p-%m.profraw');run_env=dict(env,LLVM_PROFILE_FILE=pattern)
 cmd=[('LLVM_PROFILE_FILE='+pattern) if arg.startswith('LLVM_PROFILE_FILE=') else arg for arg in cmd]
 started=time.time_ns()
 try:r=subprocess.run(cmd,env=run_env,capture_output=True,text=True,timeout=timeout)
 except subprocess.TimeoutExpired as error:
  runs.append({'name':name,'command':cmd,'started_unix_ns':started,'finished_unix_ns':time.time_ns(),'exit':'timeout','timeout_seconds':timeout,'profile_directory':str(profile_dir)})
  save(out/'runs.json',json.dumps(runs,indent=2)+'\n')
  text=lambda value:value.decode(errors='replace') if isinstance(value,bytes) else value or ''
  save(out/(name+'.log'),text(error.stdout)+text(error.stderr))
  raise
 runs.append({'name':name,'command':cmd,'started_unix_ns':started,'finished_unix_ns':time.time_ns(),'exit':r.returncode,'profile_directory':str(profile_dir)})
 save(out/'runs.json',json.dumps(runs,indent=2)+'\n')
 save(out/(name+'.log'),r.stderr if name.endswith(('-export','-lcov')) else r.stdout+r.stderr)
 assert r.returncode==0,(name,r.stdout[-1500:],r.stderr[-2000:]);return r
if not report_only:
 run('build-service',['cargo','build','--no-default-features','--features','raid5','--bin','rublk'],timeout=1800)
 r=run('build-models',['cargo','test','--no-default-features','--features','raid5','--lib','--test','raid5_abi','--test','raid5_coordination','--test','raid5_ring','--message-format=json'],timeout=900)
 objects=[]
 for line in r.stdout.splitlines():
  try:j=json.loads(line)
  except ValueError:continue
  if j.get('reason')=='compiler-artifact' and j.get('executable'):objects.append({'name':j['target']['name'],'kinds':j['target']['kind'],'test':j['profile']['test'],'path':j['executable']})
 if not any(Path(obj['path'])==service_binary for obj in objects):objects.append({'name':'rublk','kinds':['bin'],'test':False,'path':str(service_binary)})
 for obj in objects:
  if obj['name']=='raid5_ring' and obj['test']:run('root-ring',['sudo','-n','env','LLVM_PROFILE_FILE='+env['LLVM_PROFILE_FILE'],obj['path'],'--ignored'])
 save(out/'objects.json',json.dumps(objects,indent=2)+'\n')
 base=['sudo','-n','env','LLVM_PROFILE_FILE='+env['LLVM_PROFILE_FILE'],'RAID5_BINARY='+str(service_binary),'python3']
 for name,script,args in [('real-target','target_integration.py',[]),('real-failure','target_faults.py',[]),('real-broken-stderr','target_faults.py',['--broken-stderr']),('real-short-read','target_faults.py',['--short-read']),('real-short-rmw','target_faults.py',['--short-rmw']),('real-auto-reg','auto_reg_target.py',[]),('real-bootstrap','bootstrap_faults.py',[]),('real-fuse-faults','backend_fault_matrix.py',[]),('real-background-faults','sync_faults.py',[]),('real-admission','admission.py',[]),('real-sync-concurrency','sync_concurrency.py',[]),('real-repair-faults','sync_concurrency.py',['--faults']),('real-bpf-results','bpf_result_faults.py',[]),('real-command-commit','command_fault_test.py',[]),('real-command-fetch-single','command_fault_test.py',['--fetch','single','--queues','2','--depth','8']),('real-command-fetch-all','command_fault_test.py',['--fetch','all','--queues','2','--depth','8']),('real-command-held-backend','command_fault_test.py',['--hold-backend','--queues','2','--depth','8']),('real-notification-faults','notification_fault_test.py',[]),('real-arena-population','arena_population_test.py',[]),('real-metadata-startup-faults','metadata_startup_faults.py',[])]:
  run(name,[*base,'scripts/raid5/'+script,*args],timeout=300)
 run('prepare-nvme',['sudo','-n','python3','scripts/raid5/prepare_nvme.py'],timeout=300)
 run('real-nvme',[*base,'scripts/raid5/nvme_smoke.py'],timeout=300)
else:
 objects=json.loads((out/'manifest.json').read_text())['objects']
 for obj in objects:assert hashlib.sha256(Path(obj['path']).read_bytes()).hexdigest()==obj['sha256'],'coverage object changed since original collection'
profiles=list(raw.rglob('*.profraw'));assert len(profiles)>=10,len(profiles)
run('profile-permissions',['sudo','-n','chmod','-R','a+rX',str(raw)])
sysroot=Path(run('sysroot',['rustc','--print','sysroot']).stdout.strip());host=run('host',['rustc','-vV']).stdout.split('host: ')[1].splitlines()[0];llvm=sysroot/'lib/rustlib'/host/'bin';prof=out/'merged.profdata'
merge_inputs=[str(p) for p in profiles]
if report_only:
 baseline=out/('merged-before-gap-'+str(time.time_ns())+'.profdata');shutil.copy2(prof,baseline);merge_inputs.insert(0,str(baseline))
run('merge',[str(llvm/'llvm-profdata'),'merge','-sparse',*merge_inputs,'-o',str(prof)])
binary=service_binary;opts=['-instr-profile='+str(prof)]
for obj in objects:
 if Path(obj['path'])!=binary:opts+=['-object',obj['path']]
sources=[*map(str,(root/'src/raid5').glob('*.rs')),str(root/'src/raid5_cli.rs')]
vendor=[str(root/'vendor/libublk/src/io.rs'),str(root/'vendor/libublk/src/ctrl.rs'),str(root/'vendor/io-uring/src/opcode.rs'),str(root/'vendor/io-uring/src/lib.rs')]
integration=[str(root/'src/args.rs'),str(root/'src/main.rs')]
for group,files in [('target',sources),('dependencies',vendor),('integration',integration)]:
 cmd=[str(llvm/'llvm-cov'),'export',str(binary),*opts,*files]
 report=run(group+'-export',cmd+['--skip-functions']);save(out/(group+'.json'),report.stdout)
 run(group+'-html',[str(llvm/'llvm-cov'),'show',str(binary),*opts,*files,'--format=html','--output-dir='+str(out/(group+'-html')),'--show-line-counts-or-regions','--show-expansions'])
 lcov=run(group+'-lcov',cmd+['--format=lcov']);save(out/(group+'.lcov'),lcov.stdout)
 print(run(group+'-summary',[str(llvm/'llvm-cov'),'report',str(binary),*opts,*files]).stdout,flush=True)
suite_reports=json.loads((out/'suites.json').read_text()) if report_only else []
for directory in sorted(raw.iterdir()):
 suite_profiles=list(directory.glob('*.profraw'))
 if not suite_profiles:continue
 suite_prof=out/(directory.name+'.profdata')
 run(directory.name+'-merge',[str(llvm/'llvm-profdata'),'merge','-sparse',*[str(p) for p in suite_profiles],'-o',str(suite_prof)])
 suite_opts=['-instr-profile='+str(suite_prof)]
 for obj in objects:
  if Path(obj['path'])!=binary:suite_opts+=['-object',obj['path']]
 result=run(directory.name+'-export',[str(llvm/'llvm-cov'),'export',str(binary),*suite_opts,*sources,*integration,'--skip-functions'])
 save(out/(directory.name+'-coverage.json'),result.stdout)
 suite_reports=[row for row in suite_reports if row['name']!=directory.name]
 suite_reports.append({'name':directory.name,'profiles':len(suite_profiles),'coverage':directory.name+'-coverage.json','scope':'per-suite LLVM summary includes test functions; not the production-only line percentage'})
save(out/'suites.json',json.dumps(suite_reports,indent=2)+'\n')
archive=out/'objects';archive.mkdir(exist_ok=True)
for index,obj in enumerate(objects):
 copied=archive/(str(index)+'-'+Path(obj['path']).name);shutil.copy2(obj['path'],copied);obj['archived_path']=str(copied);obj['sha256']=hashlib.sha256(copied.read_bytes()).hexdigest()
def lcov_records(path):
 records={};current=None
 for line in path.read_text().splitlines():
  if line.startswith('SF:'):current=line[3:];records[current]={}
  elif line.startswith('DA:') and current:
   n,c,*_=line[3:].split(',');records[current][int(n)]=int(c)
 return records
summary=[]
for file,counts in lcov_records(out/'target.lcov').items():
 lines=Path(file).read_text().splitlines();cut=next((i+1 for i,line in enumerate(lines) if line.strip()=='#[cfg(test)]'),len(lines)+1);production={line:count for line,count in counts.items() if line<cut}
 summary.append({'file':file,'covered':sum(c>0 for c in production.values()),'executable':len(production),'uncovered_lines':[n for n,c in production.items() if c==0]})
covered=sum(f['covered'] for f in summary);total=sum(f['executable'] for f in summary)
save(out/'production-lines.json',json.dumps({'covered':covered,'executable':total,'percent':100*covered/total,'files':summary,'rule':'exclude cfg(test) module through EOF; denominator is LLVM executable DA lines','branch_support':'LLVM reports zero Rust branch sites: unsupported, not 100 percent'},indent=2)+'\n');print('PRODUCTION_LINE_COVERAGE',covered,total,100*covered/total,flush=True)
records=lcov_records(out/'dependencies.lcov');changed=[]
for crate,version,files in [('libublk','0.4.7',['src/io.rs','src/ctrl.rs']),('io-uring','0.7.12',['src/lib.rs','src/opcode.rs'])]:
 cache=next(Path('/home/xiao/.cargo/registry/cache').glob(f'*/{crate}-{version}.crate'))
 with tarfile.open(cache) as tf:
  for file in files:
   orig=tf.extractfile(f'{crate}-{version}/{file}').read().decode().splitlines();p=root/'vendor'/crate/file;new=p.read_text().splitlines();lines=[]
   for tag,a,b,c,d in difflib.SequenceMatcher(None,orig,new).get_opcodes():
    if tag in ('replace','insert'):lines.extend(range(c+1,d+1))
   executable={n:records[str(p)][n] for n in lines if n in records.get(str(p),{})}
   changed.append({'file':str(p),'changed_executable':len(executable),'covered':sum(c>0 for c in executable.values()),'uncovered_lines':[n for n,c in executable.items() if c==0],'original_crate_sha256':hashlib.sha256(cache.read_bytes()).hexdigest()})
save(out/'changed-dependencies.json',json.dumps(changed,indent=2)+'\n')
integration_records=lcov_records(out/'integration.lcov');changed_integration=[]
for file in integration:
 relative=str(Path(file).relative_to(root));original=run('integration-base-'+Path(file).stem,['git','show','c1178a43622b24e0fa491bd0f1b4a4c4bafa6614:'+relative]).stdout.splitlines();current=Path(file).read_text().splitlines();changed_lines=[]
 for tag,a,b,c,d in difflib.SequenceMatcher(None,original,current).get_opcodes():
  if tag in ('replace','insert'):changed_lines.extend(range(c+1,d+1))
 executable={n:integration_records[file][n] for n in changed_lines if n in integration_records.get(file,{})}
 changed_integration.append({'file':file,'baseline':'c1178a43622b24e0fa491bd0f1b4a4c4bafa6614','changed_executable':len(executable),'covered':sum(c>0 for c in executable.values()),'uncovered_lines':[n for n,c in executable.items() if c==0],'scope':'LLVM executable lines within inserted/replaced integration hunks; not whole CLI coverage'})
save(out/'changed-integration.json',json.dumps(changed_integration,indent=2)+'\n')
from source_manifest import fingerprint
tool_versions={name:subprocess.run(command,capture_output=True,text=True,check=True).stdout for name,command in [('rustc',['rustc','-vV']),('cargo',['cargo','-V']),('llvm-cov',[str(llvm/'llvm-cov'),'--version']),('llvm-profdata',[str(llvm/'llvm-profdata'),'--version'])]}
manifest={'full_source_sha256':fingerprint(),'tools':tool_versions,'profile_environment':{key:env[key] for key in ['RUSTFLAGS','LLVM_PROFILE_FILE','CARGO_PROFILE_DEV_DEBUG','CARGO_PROFILE_TEST_DEBUG','CARGO_INCREMENTAL','CARGO_TARGET_DIR']},'finished_unix_ns':time.time_ns(),'profiles':len(list(raw.rglob('*.profraw.gz')))+len(profiles),'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources+vendor+integration},'objects':objects,'limits':['SIGKILL workers cannot flush profiles','zero reported Rust branch sites means unsupported','optional metrics feature not included in normal-production percentage','BPF evidence is separate; no BPF source coverage percentage','performance must use uninstrumented executable']}
save(out/'manifest.json',json.dumps(manifest,indent=2)+'\n')
for profile in profiles:
 with profile.open('rb') as source,gzip.open(str(profile)+'.gz','wb',compresslevel=6) as destination:shutil.copyfileobj(source,destination)
 profile.unlink()
print('PASS coverage instrumented real workers and exports; raw profiles gzip archived',flush=True)
