#!/usr/bin/env python3
"""Guest half of externally controlled QEMU power/unplug acceptance."""
import argparse,hashlib,json,mmap,os,struct,subprocess,sys,time
from pathlib import Path
ROOT=Path('/home/xiao/rublk-raid5');WORK=Path('/home/xiao/rublk-raid5-work');STATE=WORK/'vm-faults-current';NODE='/dev/ublkb1100';S=4096
sys.path.insert(0,str(ROOT/'scripts/raid5'));from nvme_members import resolve_members
BINARY=Path(json.loads((ROOT/'docs/raid5/binaries/current.json').read_text())['binary'])
def run(cmd,timeout=60,ok=(0,)):
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout)
 if r.returncode not in ok:raise RuntimeError((cmd,r.returncode,r.stdout[-600:],r.stderr[-1200:]))
 return r
def atom(p,j):
 tmp=p.with_suffix('.tmp')
 with tmp.open('w') as f:f.write(json.dumps(j,indent=2)+'\n');f.flush();os.fsync(f.fileno())
 os.replace(tmp,p);fd=os.open(p.parent,os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)
def active(pid):
 try:return Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[0]!='Z'
 except FileNotFoundError:return False
def pattern(case,j,i):return hashlib.sha256(f'{case}:{j}:{i}'.encode()).digest()*128
def readstate():return json.loads((STATE/'current.json').read_text())
def status():
 try:return json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
 except (FileNotFoundError,ValueError):return {}
def currentdir():return Path(readstate()['directory'])
def launch(label,assemble=False,rate='0'):
 d=STATE/(label+'-'+str(time.time_ns()));d.mkdir(parents=True)
 cmd=[str(BINARY),'add','raid5','-z','--foreground','-n','1100','-q','4','-d','64','--sync-window','8','--sync-rate',rate,*(['--assemble'] if assemble else []),*map(str,resolve_members())]
 (d/'command.json').write_text(json.dumps(cmd));atom(STATE/'current.json',{'directory':str(d)})
 log=(d/'owner.log').open('w');subprocess.Popen([sys.executable,__file__,'_serve',str(d)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True);log.close()
 end=time.monotonic()+90
 while time.monotonic()<end:
  if (d/'exit.json').exists():raise RuntimeError((d/'server.log').read_text()[-1600:])
  if (d/'pid').exists():
   pid=int((d/'pid').read_text());s=status()
   if s.get('pid')==pid and s['state'] in ['RUNNING','RECOVERING'] and Path(NODE).exists():return {'directory':str(d),'pid':pid,'status':s,'startup_log':(d/'server.log').read_text()[:1800]}
  time.sleep(.02)
 raise RuntimeError('server startup timeout')
def stop():
 d=currentdir();run([str(BINARY),'del','-n','1100'],90)
 end=time.monotonic()+90
 while not (d/'exit.json').exists():
  assert time.monotonic()<end,'stop failed to drain';time.sleep(.02)
 result=json.loads((d/'exit.json').read_text());assert result['returncode']==0,result
 return result
def wait_running():
 end=time.monotonic()+300
 while time.monotonic()<end:
  s=status()
  if s.get('state')=='RUNNING':return s
  d=currentdir();assert not (d/'exit.json').exists(),(d/'server.log').read_text()[-1500:];time.sleep(.05)
 raise AssertionError('sync completion timeout')
if sys.argv[1]=='_serve':
 d=Path(sys.argv[2]);cmd=json.loads((d/'command.json').read_text());log=(d/'server.log').open('w');p=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=log);(d/'pid').write_text(str(p.pid));rc=p.wait();log.close();atom(d/'exit.json',{'returncode':rc,'finished_ns':time.time_ns()});raise SystemExit(0)
if sys.argv[1]=='_load':
 d=Path(sys.argv[2]);cmd=json.loads((d/'load-command.json').read_text());log=(d/'load.log').open('w');p=subprocess.Popen(cmd,stdout=log,stderr=log);(d/'load-pid').write_text(str(p.pid));rc=p.wait();log.close();atom(d/'load-exit.json',{'returncode':rc,'finished_ns':time.time_ns()});raise SystemExit(0)
assert os.geteuid()==0
op=sys.argv[1];out={}
if op=='boot':
 if not os.path.ismount(WORK):run(['mount','-U','3a5a167e-e4c4-45fd-9545-32b5ead9b1aa',str(WORK)])
 STATE.mkdir(exist_ok=True)
 md=Path('/sys/class/block/md127')
 if md.exists():
  assert {p.name for p in (md/'slaves').iterdir()}=={p.name for p in resolve_members()};run(['mdadm','--stop','/dev/md127'])
 out={'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kernel':run(['uname','-r']).stdout.strip(),'members':[str(x) for x in resolve_members()]}
elif op=='init':
 case=sys.argv[2];run(['python3',str(ROOT/'scripts/raid5/prepare_nvme.py')],300);out=launch(case)
 wait_running();capacity=int(run(['blockdev','--getsize64',NODE]).stdout);offsets=[0,1024*1024*1024,capacity-1024*1024];fd=os.open(NODE,os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,S)
 for j,base in enumerate(offsets):
  for i in range(256):buf[:]=pattern(case,j,i);assert os.pwrite(fd,buf,base+i*S)==S
 os.fsync(fd);os.close(fd);buf.close();checkpoint={'case':case,'offsets':offsets,'blocks_per_region':256,'capacity':capacity,'flush_acknowledged':True};atom(STATE/'checkpoint.json',checkpoint);out['checkpoint']=checkpoint
elif op=='start':out=launch(sys.argv[2],True,sys.argv[3] if len(sys.argv)>3 else '0')
elif op=='stop':out=stop()
elif op=='crash-service':
 d=currentdir();pid=int((d/'pid').read_text());os.kill(pid,9)
 end=time.monotonic()+20
 while active(pid):assert time.monotonic()<end;time.sleep(.02)
 run([str(BINARY),'del','-n','1100']);out={'crashed_service_pid':pid}
elif op=='load':
 d=currentdir();cmd=['fio','--name=vm-volatile-writes','--filename='+NODE,'--rw=randwrite','--bs=4k','--direct=1','--ioengine=io_uring','--iodepth=16','--numjobs=4','--offset=64M','--offset_increment=64M','--size=64M','--time_based=1','--runtime=300','--group_reporting=1','--output-format=json','--output='+str(d/'fio.json')];(d/'load-command.json').write_text(json.dumps(cmd));log=(d/'load-owner.log').open('w');subprocess.Popen([sys.executable,__file__,'_load',str(d)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True);log.close()
 end=time.monotonic()+10
 while not (d/'load-pid').exists():assert time.monotonic()<end;time.sleep(.02)
 time.sleep(.3);assert not (d/'load-exit.json').exists();out={'load_pid':int((d/'load-pid').read_text()),'block_stat':Path('/sys/class/block/ublkb1100/stat').read_text(),'status':status()}
elif op=='status':
 d=currentdir();out={'status':status(),'exited':json.loads((d/'exit.json').read_text()) if (d/'exit.json').exists() else None,'pid_active':active(int((d/'pid').read_text()))}
elif op=='wait-failure':
 d=currentdir();end=time.monotonic()+90
 while not (d/'exit.json').exists():assert time.monotonic()<end,'missing-member service did not exit';time.sleep(.05)
 out=json.loads((d/'exit.json').read_text());assert out['returncode']!=0,out
 if Path('/sys/class/ublk-char/ublkc1100').exists():run([str(BINARY),'del','-n','1100'])
 for member in resolve_members():
  raw=member.open('rb').read(8192);assert all(int.from_bytes(raw[o+20:o+24],'little')==2 for o in [0,4096]),'incorrect CLEAN'
 # resolve_members cannot run while a namespace is absent; caller checks raw
 # surviving metadata separately in the missing-member mode below.
elif op=='missing-failure':
 d=currentdir()
 if not (d/'load-command.json').exists() and not (d/'exit.json').exists():
  probe=subprocess.run(['dd','if='+NODE,'of=/dev/null','bs=128K','count=3','iflag=direct'],capture_output=True,text=True,timeout=20);atom(d/'idle-detection-probe.json',{'returncode':probe.returncode,'stdout':probe.stdout,'stderr':probe.stderr,'scope':'first I/O after idle unplug, not proactive health polling'});assert probe.returncode!=0
 end=time.monotonic()+90
 while not (d/'exit.json').exists():assert time.monotonic()<end,'service did not exit';time.sleep(.05)
 out=json.loads((d/'exit.json').read_text());assert out['returncode']!=0,out
 if Path('/sys/class/ublk-char/ublkc1100').exists():run([str(BINARY),'del','-n','1100'])
 from nvme_members import MEMBER_SERIALS
 records=[]
 for entry in Path('/sys/class/block').glob('nvme*n1'):
  if (entry/'device/serial').read_text().strip() in MEMBER_SERIALS:
   raw=(Path('/dev')/entry.name).open('rb').read(8192);states=[int.from_bytes(raw[o+20:o+24],'little') for o in [0,4096]];assert states==[2,2];records.append({'member':entry.name,'states':states})
 out['surviving_DIRTY']=records
 if (d/'load-command.json').exists():
  end=time.monotonic()+20
  while not (d/'load-exit.json').exists():assert time.monotonic()<end,'client did not exit';time.sleep(.05)
  out['client']=json.loads((d/'load-exit.json').read_text());assert out['client']['returncode']!=0,'client incorrectly succeeded'
 end=time.monotonic()+10;out['map_release_samples']=[]
 while True:
  out['remaining_maps']=json.loads(run(['bpftool','-j','map','show']).stdout);out['map_release_samples'].append({'monotonic':time.monotonic(),'maps':out['remaining_maps']})
  if all(m.get('name')=='cgroup_hash' for m in out['remaining_maps']):break
  assert time.monotonic()<end,'leaked target BPF maps after bounded RCU release wait';time.sleep(.1)
elif op=='refuse-missing':
 # Existing path list is captured before hot-unplug; resolver correctly refuses
 # to invent a third member. Test real CLI with those now-missing paths too.
 cmd=json.loads((currentdir()/'command.json').read_text());cmd.insert(4,'--assemble') if '--assemble' not in cmd else None
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=30);assert r.returncode!=0 and not Path('/dev/ublkb1100').exists();out={'returncode':r.returncode,'stderr':r.stderr,'stdout':r.stdout}
elif op=='verify':
 s=wait_running();cp=json.loads((STATE/'checkpoint.json').read_text());fd=os.open(NODE,os.O_RDONLY|os.O_DIRECT);buf=mmap.mmap(-1,S)
 for j,base in enumerate(cp['offsets']):
  for i in range(256):assert os.preadv(fd,[buf],base+i*S)==S and buf[:]==pattern(cp['case'],j,i),('checkpoint content',j,i)
 os.close(fd);buf.close();stop();tool=STATE/'check-physical';run(['clang-19','-O2','-Wall','-Wextra','-Werror',str(ROOT/'scripts/raid5/check_physical.c'),'-o',str(tool)]);r=run([str(tool),*map(str,resolve_members())],300);out={'checkpoint_blocks_verified':768,'physical_parity':r.stdout,'final_status':s,'metadata':'normal CLEAN stop'}
elif op=='shutdown':
 subprocess.Popen(['sh','-c','sleep 1; systemctl poweroff'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True);out={'scheduled_graceful_shutdown':True}
elif op=='slot-power':
 slot=Path('/sys/bus/pci/slots')/sys.argv[2]/'power';before=slot.read_text();slot.write_text('1\n');out={'slot':str(slot),'before':before,'after':slot.read_text()}
elif op=='tool-identity':out={'path':__file__,'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'binary':str(BINARY),'binary_sha256':hashlib.sha256(BINARY.read_bytes()).hexdigest()}
elif op=='kernel':out={'kernel_log':run(['dmesg']).stdout}
else:raise RuntimeError(op)
print(json.dumps(out),flush=True)
