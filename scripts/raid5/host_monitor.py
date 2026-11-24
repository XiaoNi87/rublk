#!/usr/bin/env python3
"""Read-only host evidence during virtualized benchmarks; never controls other VMs."""
import argparse,hashlib,json,os,socket,subprocess,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--vm-dir',type=Path,required=True);p.add_argument('--key',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
ssh=['ssh','-i',str(a.key),'-oBatchMode=yes','-oConnectTimeout=3','-oUserKnownHostsFile='+str(a.key.parent/'known_hosts'),'-p2222','xiao@127.0.0.1']
def run(cmd):
 try:
  r=subprocess.run(cmd,capture_output=True,text=True,timeout=8);return {'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
 except Exception as e:return {'error':repr(e)}
def blockstats():
 s=socket.socket(socket.AF_UNIX);s.settimeout(3)
 try:
  s.connect(str(a.vm_dir/'final-validation.qmp'));f=s.makefile('rwb');f.readline()
  for n,c in enumerate(['qmp_capabilities','query-blockstats']):
   f.write((json.dumps({'execute':c,'id':n})+'\n').encode());f.flush()
   while True:
    r=json.loads(f.readline())
    if r.get('id')==n:break
  return r
 except Exception as e:return {'error':repr(e)}
 finally:s.close()
initial={'started_ns':time.time_ns(),'scope':'read-only host snapshots; no VM control','monitor_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'os':run(['sw_vers']),'memory':run(['sysctl','hw.memsize']),'boot_script_sha256':hashlib.sha256((a.vm_dir/'boot-debian.sh').read_bytes()).hexdigest(),'qemu_sha256':hashlib.sha256(Path('/opt/homebrew/bin/qemu-system-aarch64').read_bytes()).hexdigest()};(a.output/'initial.json').write_text(json.dumps(initial,indent=2)+'\n')
caffeinate=subprocess.Popen(['caffeinate','-i','-w',str(os.getpid())],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
try:
 with (a.output/'samples.jsonl').open('a',buffering=1) as out:
  while True:
   procs=run(['ps','-axo','pid,rss,%cpu,command']);procs['stdout']='\n'.join(x for x in procs.get('stdout','').splitlines() if 'qemu-system-aarch64' in x)
   row={'unix_ns':time.time_ns(),'swap':run(['sysctl','vm.swapusage']),'vm_stat':run(['vm_stat']),'qemu_processes':procs,'blockstats':blockstats(),'guest_completion':run([*ssh,"python3 -c 'from pathlib import Path;p=Path(\"/home/xiao/rublk-raid5-work/validation-final-control/exit.json\");print(p.read_text() if p.exists() else \"pending\")'"])};out.write(json.dumps(row)+'\n')
   if row['guest_completion'].get('rc')==0 and row['guest_completion']['stdout'].strip()!='pending':break
   time.sleep(45)
finally:caffeinate.terminate()
