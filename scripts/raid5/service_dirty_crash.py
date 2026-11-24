#!/usr/bin/env python3
"""Real frozen-service --assemble DIRTY startup cutpoints (not an example).
Bpftrace identifies member FDs from actual openat results, injects SIGKILL at
pwrite/fsync entry/exit, then the same service retries through CLI/controller.
Retains cache. No physical power-loss or sector atomicity claim.
"""
import hashlib,json,mmap,os,re,runpy,shlex,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
os.environ['BPFTRACE_MAX_STRLEN']='256'
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ['RAID5_BINARY']).resolve();expected_sha='f103e098fbb8d945fcf56d4c15e131cde55afb5cacccbf42d6579455cfafb9ed'
assert hashlib.sha256(binary.read_bytes()).hexdigest()==expected_sha
node=Path('/dev/ublkb1100');kernel_node=Path('/sys/class/ublk-char/ublkc1100');assert not kernel_node.exists()
P=1048576;C=65536;N=16;CAP=2*N*C;out=root/'docs/raid5/service-dirty-crash'/str(time.time_ns());out.mkdir(parents=True)
directory=Path(tempfile.mkdtemp(prefix='raid5-service-dirty-',dir='/home/xiao'));base=directory/'base';base.mkdir();members=[base/f'member{i}' for i in range(3)];expected=bytearray(CAP);results=[];logs=[];server=None
for p in members:
 with p.open('xb') as h:h.truncate(P+N*C)

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(cmd,timeout=30):
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
 assert r.returncode==0,(cmd,r.returncode,r.stdout,r.stderr)
 return r

def wait(f,label,seconds=20):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  value=f()
  if value:return value
  time.sleep(.01)
 raise AssertionError('timeout '+label)

def state():
 try:return json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
 except (FileNotFoundError,ValueError):return {}

def command(paths):return [str(binary),'add','raid5','-z','--test-files','--foreground','--assemble','-n','1100','-q','2','-d','8',*map(str,paths)]
def start(paths,name,create=False):
 h=(out/(name+'.log')).open('w');logs.append(h);cmd=command(paths)
 if create:cmd.remove('--assemble')
 p=subprocess.Popen(cmd,stdout=h,stderr=h)
 def ready():
  assert p.poll() is None,(out/(name+'.log')).read_text()
  s=state();return s if s.get('pid')==p.pid and s.get('state')=='RUNNING' else False
 s=wait(ready,'service ready');return p,s

def deleted():
 if kernel_node.exists():run([str(binary),'del','-n','1100'])
 wait(lambda:not kernel_node.exists(),'kernel device deleted')

def stop(p):
 deleted();assert p.wait(timeout=30)==0

def aligned_io(fd,offset,data=None,size=4096):
 buf=mmap.mmap(-1,len(data) if data is not None else size)
 try:
  if data is not None:buf[:]=data;assert os.pwrite(fd,buf,offset)==len(data)
  else:assert os.preadv(fd,[buf],offset)==size;return buf[:]
 finally:buf.close()

def crc_record(raw):
 assert len(raw)==4096
 b=bytearray(raw);stored=int.from_bytes(b[16:20],'little');b[16:20]=bytes(4);crc=0xffffffff
 for byte in b:
  crc^=byte
  for _ in range(8):crc=(crc>>1)^(0x82f63b78 if crc&1 else 0)
 if stored!=(crc^0xffffffff):return None
 return dict(array=raw[24:40].hex(),members=raw[40:88].hex(),member=int.from_bytes(raw[88:92],'little'),generation=int.from_bytes(raw[128:136],'little'),state=int.from_bytes(raw[20:24],'little'))

def logical():
 fd=os.open(node,os.O_RDONLY|os.O_DIRECT)
 try:
  for off in range(0,CAP,2*C):assert aligned_io(fd,off,size=2*C)==expected[off:off+2*C],('logical mismatch',off)
 finally:os.close(fd)

def physical(paths):
 files=[p.read_bytes() for p in paths]
 for stripe in range(N):
  parity=2-stripe%3;a=(parity+1)%3;b=(parity+2)%3;o=P+stripe*C
  aa=files[a][o:o+C];bb=files[b][o:o+C]
  assert aa+bb==expected[stripe*2*C:(stripe+1)*2*C],('physical data mismatch',stripe)
  assert bytes(x^y for x,y in zip(aa,bb))==files[parity][o:o+C],('physical parity mismatch',stripe)
 assert all(f[12288:P]==bytes(P-12288) for f in files),'prefix canary changed'

def map_ids():return {x['id'] for x in json.loads(run(['bpftool','-j','map','show']).stdout)}
initial_maps=map_ids()
artifacts={'service':{'path':str(binary),'sha256':expected_sha,'role':'real rublk CLI/controller service for ALL startup cutpoints and retries'},'injector':{'path':str(Path(shutil.which('bpftrace')).resolve()),'sha256':sha(Path(shutil.which('bpftrace')).resolve()),'role':'external syscall tracepoint SIGKILL injection'},'harness':{'path':str(Path(__file__).resolve()),'sha256':sha(Path(__file__).resolve())}}
source_manifest=root/'docs/raid5/source-manifest.json';artifacts['source_manifest']={'path':str(source_manifest),'sha256':sha(source_manifest),'archive_sha256':json.loads(source_manifest.read_text())['archive_sha256']}
source_files=runpy.run_path(str(root/'scripts/raid5/source_manifest.py'))['fingerprint']()
(out/'source-files-before-run.json').write_text(json.dumps(source_files,indent=2)+'\n')
artifacts['source_files_before_run']={'path':str(out/'source-files-before-run.json'),'sha256':sha(out/'source-files-before-run.json')}
artifacts['service']['build_source_manifest']={'path':str(root/'docs/raid5/command-cqe-source-manifest.json'),'sha256':sha(root/'docs/raid5/command-cqe-source-manifest.json')}
(out/'artifacts-before-run.json').write_text(json.dumps(artifacts,indent=2)+'\n')

def trace_code(paths,syscall=None,edge=None,wanted=None,ordinal=None):
 # Use member identity rather than preflight fd numbers: descriptor allocation
 # may differ across service invocations. Member values are index+1.
 clauses=['BEGIN { printf("SERVICE_CPID %d\\n",cpid); }', 'tracepoint:sched:sched_process_exec /pid == cpid/ { printf("SERVICE_EXEC %s\\n",str(args.filename)); }']
 clauses+=['kprobe:security_bprm_check /pid == cpid/ { printf("SERVICE_BPRM %s\\n",str(((struct linux_binprm *)arg0)->filename,256)); }']
 clauses+=['tracepoint:syscalls:sys_enter_openat /pid == cpid/ { @opening[tid]=str(args.filename); }']
 body=''
 for m,path in enumerate(paths):body+=f'if (@opening[tid] == "{path}") {{ @member[args.ret]={m+1}; }} '
 clauses+=[f'tracepoint:syscalls:sys_exit_openat /pid == cpid && args.ret >= 0/ {{ {body} delete(@opening[tid]); }}']
 for call,kind in [('pwrite64','writes'),('fsync','flushes')]:
  body=f'$m=@member[args.fd]; @{kind}[$m]=count(); @fd_{call}[tid]=args.fd;'
  if call=='pwrite64':body+=' @write_offsets[$m,args.pos]=count(); @write_sizes[$m,args.count]=count();'
  if call==syscall and edge=='enter':body+=f' if ($m == {wanted+1}) {{ @ordinal=@ordinal+1; if (@ordinal == {ordinal}) {{ printf("KILL_BOUNDARY member=%d edge=enter\\n",$m); signal("SIGKILL"); }} }}'
  clauses+=[f'tracepoint:syscalls:sys_enter_{call} /pid == cpid && @member[args.fd] > 0/ {{ {body} }}']
  if call==syscall and edge=='exit':
   clauses+=[f'tracepoint:syscalls:sys_exit_{call} /pid == cpid && @member[@fd_{call}[tid]] == {wanted+1}/ {{ @ordinal=@ordinal+1; if (@ordinal == {ordinal}) {{ printf("KILL_BOUNDARY member={wanted+1} edge=exit res=%d\\n",args.ret); signal("SIGKILL"); }} }}']
 return '\n'.join(clauses)+'\n'

def check_trace(text,inject=False):
 assert f'SERVICE_BPRM {binary}' in text and 'SERVICE_EXEC ' in text,text[-2500:]
 m=re.search(r'SERVICE_CPID (\d+)',text);assert m
 if inject:assert 'KILL_BOUNDARY' in text and 'RAID5 STARTING' not in text,'cutpoint failed or service published before kill'
 return int(m[1])

try:
 server,_=start(members,'baseline',True)
 fd=os.open(node,os.O_RDWR|os.O_DIRECT)
 try:
  for off,size in [(0,2*C),(2*C,2*C),(4*C,2*C),(6*C+8192,4096),(9*C-4096,12288),(CAP-4096,4096)]:
   data=hashlib.shake_256(f'service-dirty:{off}:{size}'.encode()).digest(size);aligned_io(fd,off,data);expected[off:off+size]=data
  os.fsync(fd)
 finally:os.close(fd)
 logical();stop(server);physical(members)
 base_records=[crc_record(p.read_bytes()[:4096]) for p in members];assert all(r and r['state']==1 for r in base_records)
 base_data_hashes=[hashlib.sha256(p.read_bytes()[P:]).hexdigest() for p in members]
 # Discover true service startup counts, ending by SIGKILL at LIVE so CLEAN
 # shutdown syscalls cannot contaminate the DIRTY preflight counts.
 work=directory/'preflight';work.mkdir();paths=[work/p.name for p in members]
 for src,dst in zip(members,paths):shutil.copyfile(src,dst)
 bt=out/'preflight.bt';bt.write_text(trace_code(paths));h=(out/'preflight.log').open('w')
 tracer=subprocess.Popen(['bpftrace','--unsafe','-q',str(bt),'-c',shlex.join(command(paths))],stdout=h,stderr=h)
 def preflight_ready():
  assert tracer.poll() is None
  s=state()
  if s.get('state')!='RUNNING':return False
  try:cmd=Path(f"/proc/{s['pid']}/cmdline").read_bytes()
  except FileNotFoundError:return False
  return s if str(paths[0]).encode() in cmd else False
 s=wait(preflight_ready,'traced service ready');assert Path(f"/proc/{s['pid']}/exe").resolve()==binary;os.kill(s['pid'],9);tracer.wait(timeout=30);h.close();text=(out/'preflight.log').read_text();assert check_trace(text)==s['pid'];deleted()
 counts={kind:{int(m):int(n) for m,n in re.findall('@'+kind+r'\[(\d+)\]: (\d+)',text)} for kind in ['writes','flushes']}
 assert counts=={'writes':{1:2,2:2,3:2},'flushes':{1:4,2:4,3:4}},counts
 (out/'preflight-counts.json').write_text(json.dumps(counts,indent=2)+'\n');shutil.rmtree(work)
 for syscall,kind in [('pwrite64','writes'),('fsync','flushes')]:
  for edge in ['enter','exit']:
   for member in range(3):
    for ordinal in range(1,counts[kind][member+1]+1):
     name=f'{syscall}-{edge}-member{member}-{ordinal}';work=directory/'work';work.mkdir();paths=[work/p.name for p in members];success=False
     for src,dst in zip(members,paths):shutil.copyfile(src,dst)
     try:
      bt=out/(name+'.bt');bt.write_text(trace_code(paths,syscall,edge,member,ordinal))
      r=subprocess.run(['bpftrace','--unsafe','-q',str(bt),'-c',shlex.join(command(paths))],capture_output=True,text=True,timeout=30);text=r.stdout+r.stderr;(out/(name+'.log')).write_text(text);pid=check_trace(text,True);assert r.returncode==0,(name,r.returncode,text)
      deleted();before=[]
      for m,p in enumerate(paths):
       raw=p.read_bytes();(out/f'{name}-member{m}.bin').write_bytes(raw[:12288]);slots=[crc_record(raw[o:o+4096]) for o in [0,4096]]
       assert any(x and x['array']==base_records[m]['array'] and x['members']==base_records[m]['members'] and x['member']==m for x in slots),(name,m,slots)
       assert raw[8192:12288]==members[m].read_bytes()[8192:12288],'immutable seal changed'
       assert hashlib.sha256(raw[P:]).hexdigest()==base_data_hashes[m],'startup modified data before DIRTY admission'
       before.append(slots)
      clean_before=all(x and x['state']==1 for member_slots in before for x in member_slots) and len({x['generation'] for member_slots in before for x in member_slots if x})==1
      server,snapshot=start(paths,name+'-retry')
      if not clean_before:assert snapshot['sync']['started_unix_ns']>0,'mixed DIRTY incorrectly skipped sync'
      logical();stop(server);physical(paths)
      for p in paths:assert all(crc_record(p.read_bytes()[o:o+4096])['state']==1 for o in [0,4096])
      wait(lambda:map_ids()==initial_maps,'BPF maps reclaimed')
      results.append({'boundary':name,'service_pid':pid,'service_exec':str(binary),'service_sha256':expected_sha,'before':before,'baseline_clean':clean_before,'retry':'same real service --assemble through RUNNING, full logical/fsynced epoch,16 physical stripes and CLEAN stop','no_pre_admission_data_write':True,'no_map_or_device_leak':True,'passed':True});success=True;print('PASS',name,flush=True)
     finally:
      if kernel_node.exists():deleted()
      if server is not None and server.poll() is None:server.wait(timeout=30)
      if success:shutil.rmtree(work)
      (out/'manifest.json').write_text(json.dumps({'complete':len(results)==36,'scope':'real frozen service DIRTY startup boundaries, retained-cache process crash, not metadata probe or physical powercut','artifacts':artifacts,'preflight':counts,'baseline_data_hashes':base_data_hashes,'results':results},indent=2)+'\n')
finally:
 if kernel_node.exists():deleted()
 if server is not None and server.poll() is None:server.wait(timeout=30)
 for h in logs:h.close()
 if len(results)==36:shutil.rmtree(directory)
 print('EVIDENCE',out,flush=True)
