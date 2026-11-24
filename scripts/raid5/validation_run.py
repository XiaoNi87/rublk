#!/usr/bin/env python3
"""Sequential virtualized acceptance runs; no build or tracing during sampling."""
import json,os,signal,subprocess,sys,time,hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[2];os.chdir(root)
assert os.geteuid()==0
frozen=json.loads((root/'docs/raid5/binaries/current.json').read_text());binary=frozen['binary'];assert hashlib.sha256(Path(binary).read_bytes()).hexdigest()==frozen['sha256']
assert Path('/proc/sys/kernel/bpf_stats_enabled').read_text().strip()=='0'
assert not Path('/dev/ublkb1100').exists() and not Path('/sys/class/block/md1100/md').exists()
out=root/'docs/raid5'/('validation-'+str(time.time_ns()));out.mkdir();state={'started_unix_ns':time.time_ns(),'scope':'QEMU virtual NVMe only; physical acceptance deferred','binary':frozen,'complete':False,'stages':[]};child=None

def save():
 p=out/'state.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(state,indent=2)+'\n');os.replace(tmp,p)
def interrupted(signum,frame):raise KeyboardInterrupt('validation pause requested')
signal.signal(signal.SIGTERM,interrupted)
save();print('VALIDATION_DIRECTORY',out,flush=True)
try:
 stages=[(f'supplemental-{band}',['--recovery-only','--recovery-rate','128MiB','--recovery-duration','30','--recovery-band',band]) for band in ['mixed','behind','ahead']]
 stages.append(('supplemental-paused',['--recovery-only','--recovery-rate','128MiB','--recovery-duration','90','--recovery-pause-after','5','--recovery-pause-seconds','20']))
 stages.append(('full-steady-and-native-recovery',['--recovery','--recovery-duration','30']))
 for name,arguments in stages:
  cmd=['python3','scripts/raid5/benchmark.py','--binary',binary,*arguments];entry={'name':name,'command':cmd,'started_unix_ns':time.time_ns(),'complete':False};state['stages'].append(entry);save()
  with (out/(name+'.log')).open('w') as log:
   child=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1)
   for line in child.stdout:
    log.write(line);log.flush();entry['last_output']=line.strip();entry['last_progress_unix_ns']=time.time_ns();save();print(name,line.strip(),flush=True)
    if line.startswith('BENCHMARK_DIRECTORY '):entry['benchmark_directory']=line.strip().split(' ',1)[1]
   rc=child.wait();child=None;entry['exit']=rc;entry['finished_unix_ns']=time.time_ns();save();assert rc==0,(name,rc)
  directory=entry['benchmark_directory'];manifest=json.loads((Path(directory)/'manifest.json').read_text());assert manifest.get('complete')
  subprocess.run(['python3','scripts/raid5/benchmark_report.py',directory],check=True)
  for recovery in manifest.get('recovery_results',[]):
   subprocess.run(['python3','scripts/raid5/recovery_window_report.py',str(Path(directory)/recovery)],check=True)
  entry['complete']=True;save()
 state['complete']=True
except BaseException as e:
 state['interruption_or_error']=repr(e)
 if child is not None and child.poll() is None:
  child.terminate()
  try:child.wait(timeout=150)
  except subprocess.TimeoutExpired:state['cleanup_timeout']=True;raise
 raise
finally:
 state['finished_unix_ns']=time.time_ns();save()
 print('VALIDATION_FINAL',out,state['complete'],flush=True)
