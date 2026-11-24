#!/usr/bin/env python3
"""Kill REAL target at every CLEAN pwrite/fsync entry/exit per member.
Dirty-entry boundaries are metadata_restart.py. Cache stays present; no actual
powercut claim. Each retry checks a pre-crash fsynced epoch and full parity.
"""
import hashlib,json,os,re,shlex,shutil,subprocess,tempfile,time
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root);binary=Path(os.environ['RAID5_BINARY']).resolve();node=Path('/dev/ublkb1100');assert not node.exists()
out=root/'docs/raid5/clean-metadata-crash'/str(time.time_ns());out.mkdir(parents=True);results=[];P=1048576;C=65536;N=16
stopper="""import os,subprocess,sys,time
subprocess.run([sys.argv[1],'del','-n','1100'],capture_output=True,timeout=20)
for _ in range(1000):
 try:
  if open('/proc/'+sys.argv[2]+'/stat').read().split()[2]=='Z':break
 except FileNotFoundError:break
 time.sleep(.01)
else:raise AssertionError('server did not exit')
"""
stopper_path=out/'stop_and_wait.py';stopper_path.write_text(stopper)
def wait(f,label):
 for _ in range(2000):
  if f():return
  time.sleep(.01)
 raise AssertionError('timeout '+label)
def status():return json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
def run(cmd):return subprocess.run(cmd,check=True,capture_output=True,text=True,timeout=30)
def crc_record(raw):
 b=bytearray(raw);stored=int.from_bytes(b[16:20],'little');b[16:20]=bytes(4);crc=0xffffffff
 for byte in b:
  crc^=byte
  for _ in range(8):crc=(crc>>1)^(0x82f63b78 if crc&1 else 0)
 return stored==(crc^0xffffffff)
for syscall,total in [('pwrite64',2),('fsync',3)]:
 for boundary in ['enter','exit']:
  for member in range(3):
   for ordinal in range(1,total+1):
    name=f'{syscall}-{boundary}-member{member}-{ordinal}';directory=Path(tempfile.mkdtemp(prefix='raid5-clean-crash-',dir='/home/xiao'));paths=[directory/f'm{i}' for i in range(3)];logs=[];server=None;ok=False
    for p in paths:
     with p.open('xb') as h:h.truncate(P+N*C)
    def start(assemble=False):
     h=(out/f'{name}-session-{len(logs)}.log').open('w');logs.append(h)
     cmd=[str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q','2','-d','8']
     if assemble:cmd+=['--assemble']
     p=subprocess.Popen(cmd+list(map(str,paths)),stdout=h,stderr=h)
     def ready():
      assert p.poll() is None
      try:s=status()
      except (ValueError,FileNotFoundError):return False
      return s['pid']==p.pid and s['state']=='RUNNING'
     wait(ready,'startup');return p
    try:
     server=start()
     code="import os,mmap;f=os.open('/dev/ublkb1100',os.O_RDWR|os.O_DIRECT);b=mmap.mmap(-1,4096);b[:]=bytes([165])*4096;assert os.pwrite(f,b,0)==4096;os.fsync(f);os.close(f);b.close()";run(['python3','-c',code])
     fds=[int(p.name) for p in Path(f'/proc/{server.pid}/fd').iterdir() if p.resolve()==paths[member]];assert len(fds)==1,fds;fd=fds[0]
     kill=f'@n = @n + 1; if (@n == {ordinal}) {{ printf("KILL_BOUNDARY\\n"); signal("SIGKILL"); }}'
     code=(f'tracepoint:syscalls:sys_enter_{syscall} /pid == {server.pid} && args.fd == {fd}/ {{ {kill} }}' if boundary=='enter' else f'tracepoint:syscalls:sys_enter_{syscall} /pid == {server.pid}/ {{ @fd[tid] = args.fd; }} tracepoint:syscalls:sys_exit_{syscall} /pid == {server.pid} && @fd[tid] == {fd}/ {{ {kill} }}')
     bt=out/f'{name}.bt';bt.write_text(code+'\n')
     trace=subprocess.run(['bpftrace','--unsafe','-q',str(bt),'-c',shlex.join(['python3',str(stopper_path),str(binary),str(server.pid)])],capture_output=True,text=True,timeout=40);(out/f'{name}.log').write_text(trace.stdout+trace.stderr);assert trace.returncode==0 and 'KILL_BOUNDARY' in trace.stdout,(trace.stdout,trace.stderr);assert server.wait(timeout=20)!=0
     if node.exists():run([str(binary),'del','-n','1100'])
     slots=[]
     for m,p in enumerate(paths):
      raw=p.read_bytes()[:12288];(out/f'{name}-member{m}.bin').write_bytes(raw);valid=[crc_record(raw[o:o+4096]) for o in [0,4096]];assert any(valid),(name,m)
      slots.append([int.from_bytes(raw[o+20:o+24],'little') if v else None for o,v in zip([0,4096],valid)])
     server=start(True)
     code="import os,mmap;f=os.open('/dev/ublkb1100',os.O_RDONLY|os.O_DIRECT);b=mmap.mmap(-1,4096);assert os.preadv(f,[b],0)==4096;assert b[:]==bytes([165])*4096;os.close(f);b.close()";run(['python3','-c',code])
     run([str(binary),'del','-n','1100']);assert server.wait(timeout=20)==0
     files=[p.read_bytes() for p in paths]
     for st in range(N):
      p=2-st%3;a=(p+1)%3;b=(p+2)%3;o=P+st*C;expected=bytearray(2*C)
      if st==0:expected[:4096]=bytes([165])*4096
      assert files[a][o:o+C]+files[b][o:o+C]==expected,(name,st,'content')
      assert bytes(x^y for x,y in zip(files[a][o:o+C],files[b][o:o+C]))==files[p][o:o+C],(name,st,'parity')
     results.append(dict(boundary=name,before_retry_states=slots,durable_epoch='4096 bytes165 plus all remaining zero',physical_stripes=N,passed=True));ok=True;print('PASS',name,flush=True)
    finally:
     if node.exists():run([str(binary),'del','-n','1100'])
     if server is not None and server.poll() is None:server.wait(timeout=30)
     for h in logs:h.close()
     if ok:shutil.rmtree(directory)
     (out/'manifest.json').write_text(json.dumps(dict(complete=len(results)==30,results=results,binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),scope='actual CLEAN process-crash boundaries; retained cache; no powercut'),indent=2));print(out,flush=True)
