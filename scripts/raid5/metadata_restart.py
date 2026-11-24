#!/usr/bin/env python3
"""Real process death at metadata syscall boundaries, retaining device/page cache.
Not a hardware power-cut or torn-sector emulator; model tests cover those faults.
"""
import hashlib,json,os,re,shlex,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/release/rublk'))).resolve();probe=(root/'target/release/examples/raid5_metadata_process').resolve()
assert not Path('/dev/ublkb1100').exists()
out=root/'docs/raid5/metadata-restart'/str(time.time_ns());out.mkdir(parents=True)
directory=Path(tempfile.mkdtemp(prefix='raid5-metadata-restart-',dir='/home/xiao'));base=directory/'base';base.mkdir();members=[base/f'member{i}' for i in range(3)]
for p in members:
 with p.open('xb') as f:f.truncate(17*1048576)
log=(out/'baseline.log').open('w');server=subprocess.Popen([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','1','-d','1',*map(str,members)],stdout=log,stderr=log)
try:
 for _ in range(1000):
  assert server.poll() is None
  try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
  except (ValueError,FileNotFoundError):s={}
  if s.get('pid')==server.pid and s.get('state')=='RUNNING':break
  time.sleep(.01)
 else:raise AssertionError('baseline startup')
finally:
 if Path('/dev/ublkb1100').exists():subprocess.run([str(binary),'del','-n','1100'],check=True,capture_output=True,timeout=20)
 assert server.wait(timeout=20)==0;log.close()

def crc_record(raw):
 if len(raw)!=4096:return None
 b=bytearray(raw);stored=int.from_bytes(b[16:20],'little');b[16:20]=bytes(4);crc=0xffffffff
 for byte in b:
  crc^=byte
  for _ in range(8):crc=(crc>>1)^(0x82f63b78 if crc&1 else 0)
 if stored!=(crc^0xffffffff):return None
 return {'array':raw[24:40].hex(),'members':raw[40:88].hex(),'index':int.from_bytes(raw[88:92],'little'),'generation':int.from_bytes(raw[128:136],'little'),'state':int.from_bytes(raw[20:24],'little')}
identity=[crc_record(p.read_bytes()[:4096]) for p in members];assert all(r and r['state']==1 for r in identity)
results=[]
# Preflight discovers actual descriptors/counts. Per-fd counters below avoid
# lost updates from parallel members incrementing one non-atomic BPF map value.
work=directory/'preflight';work.mkdir();paths=[work/p.name for p in members]
for source,dest in zip(members,paths):shutil.copyfile(source,dest)
bt=out/'preflight.bt';bt.write_text('tracepoint:syscalls:sys_enter_pwrite64 /pid == cpid/ { @writes[args.fd] = count(); } tracepoint:syscalls:sys_enter_fsync /pid == cpid/ { @flushes[args.fd] = count(); }\n')
r=subprocess.run(['bpftrace','-q',str(bt),'-c',shlex.join([str(probe),*map(str,paths)])],capture_output=True,text=True,timeout=30);(out/'preflight.log').write_text(r.stdout+r.stderr);assert r.returncode==0,r.stderr
counts={kind:{int(fd):int(n) for fd,n in re.findall('@'+kind+r'\[(\d+)\]: (\d+)',r.stdout)} for kind in ('writes','flushes')};assert len(counts['writes'])==3 and all(n==2 for n in counts['writes'].values()),counts;assert counts['writes'].keys()==counts['flushes'].keys(),counts
shutil.rmtree(work)
for syscall,kind in [('pwrite64','writes'),('fsync','flushes')]:
 for boundary in ('enter','exit'):
  for fd,total in sorted(counts[kind].items()):
   for ordinal in range(1,total+1):
    name=f'{syscall}-{boundary}-fd{fd}-{ordinal}';work=directory/'work';work.mkdir();paths=[work/p.name for p in members]
    for source,dest in zip(members,paths):shutil.copyfile(source,dest)
    kill=f'@n = @n + 1; if (@n == {ordinal}) {{ printf("KILL_BOUNDARY\\n"); signal("SIGKILL"); }}'
    code=(f'tracepoint:syscalls:sys_enter_{syscall} /pid == cpid && args.fd == {fd}/ {{ {kill} }}' if boundary=='enter' else f'tracepoint:syscalls:sys_enter_{syscall} /pid == cpid/ {{ @fd[tid] = args.fd; }} tracepoint:syscalls:sys_exit_{syscall} /pid == cpid && @fd[tid] == {fd}/ {{ {kill} }}')
    bt=out/f'{name}.bt';bt.write_text(code+'\n')
    r=subprocess.run(['bpftrace','--unsafe','-q',str(bt),'-c',shlex.join([str(probe),*map(str,paths)])],capture_output=True,text=True,timeout=30);(out/f'{name}.log').write_text(r.stdout+r.stderr);assert 'KILL_BOUNDARY' in r.stdout,(name,r.stdout,r.stderr)
    before=[]
    for i,p in enumerate(paths):
     raw=p.read_bytes()[:12288];(out/f'{name}-member{i}.bin').write_bytes(raw);slots=[crc_record(raw[offset:offset+4096]) for offset in (0,4096)]
     assert any(s and s['array']==identity[i]['array'] and s['members']==identity[i]['members'] and s['index']==i for s in slots),(name,i,slots)
     before.append(slots)
    # Same host/cache, no reboot: Session must fence and flush baseline before
    # choosing/overwriting a survivor. It may start from CLEAN or mixed DIRTY.
    retry=subprocess.run([str(probe),*map(str,paths)],capture_output=True,text=True,timeout=20);(out/f'{name}-retry.log').write_text(retry.stdout+retry.stderr);assert retry.returncode==0,(name,retry.stderr)
    after=[]
    for i,p in enumerate(paths):
     raw=p.read_bytes()[:8192];slots=[crc_record(raw[offset:offset+4096]) for offset in (0,4096)];assert all(s and s['state']==2 and s['array']==identity[i]['array'] for s in slots),(name,i,slots);after.append(slots)
    results.append({'boundary':name,'before':before,'after':after});shutil.rmtree(work)
    (out/'manifest.json').write_text(json.dumps({'complete':False,'scope':'process-only crash retaining cache; no physical powercut','binary_sha256':hashlib.sha256(probe.read_bytes()).hexdigest(),'results':results},indent=2)+'\n');print('PASS',name,flush=True)
m=json.loads((out/'manifest.json').read_text());m['complete']=True;(out/'manifest.json').write_text(json.dumps(m,indent=2)+'\n');print('PASS',out,flush=True)
