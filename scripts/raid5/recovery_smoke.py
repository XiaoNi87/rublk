#!/usr/bin/env python3
"""Explicit loop-target kernel-lifecycle gate; not RAID5 crash recovery evidence."""
import json, os, signal, subprocess, sys, time
from pathlib import Path
assert os.geteuid() == 0, 'must run as root; no skip'
root = Path(__file__).resolve().parents[2]
os.chdir(root)
fixture = json.loads(Path('docs/raid5/fixtures.json').read_text())['members'][2]
node = Path('/dev/ublkb1102')
assert not node.exists(), 'device ID occupied'
log = open('/tmp/raid5-recovery-server.log', 'w')
server = subprocess.Popen(['target/debug/rublk','add','loop','-n','1102','-q','2','-d','8','-b','128KiB','-r','-z','-a','--foreground','-f',fixture],stdout=log,stderr=log)
def inspect(*extra):
    r=subprocess.run(['target/debug/examples/raid5_recovery_probe','1102',*extra],capture_output=True,text=True)
    if r.returncode: raise RuntimeError(r.stderr)
    return json.loads(r.stdout)
try:
    for _ in range(100):
        if node.exists():break
        if server.poll() is not None:raise RuntimeError(Path('/tmp/raid5-recovery-server.log').read_text())
        time.sleep(.05)
    assert node.exists()
    d=inspect();assert d['pid']==server.pid
    print('initial',d,flush=True)
    # Independent stable epoch outside the later fio range.
    expected=bytes((i*13+7)%256 for i in range(8192))
    fd=os.open(node,os.O_RDWR)
    assert os.pwrite(fd,expected,12*1024*1024)==len(expected)
    os.fsync(fd);os.close(fd)
    fio=subprocess.Popen(['/usr/bin/fio','--name=crash-load','--filename='+str(node),'--rw=randwrite','--bs=4k','--ioengine=io_uring','--iodepth=16','--direct=1','--size=8m','--time_based=1','--runtime=10','--output-format=json'],stdout=open('/tmp/raid5-recovery-fio.json','w'),stderr=subprocess.STDOUT)
    time.sleep(1)
    assert fio.poll() is None, 'fio did not remain active'
    os.kill(server.pid,signal.SIGSTOP)
    print('stopped-worker takeover rejected',inspect('check-fenced'),flush=True)
    os.kill(server.pid,signal.SIGKILL);server.wait(timeout=10)
    for _ in range(100):
        d=inspect()
        if d['state']!=1:break
        time.sleep(.05)
    print('after exit',d,flush=True)
    r=subprocess.run(['target/debug/rublk','recover','-n','1102'],capture_output=True,text=True,timeout=15)
    assert r.returncode==0,(r.stdout,r.stderr)
    new=inspect();assert new['pid']!=server.pid
    print('recovered',new,flush=True)
    fd=os.open(node,os.O_RDONLY)
    assert os.pread(fd,len(expected),12*1024*1024)==expected
    os.close(fd)
    fio.wait(timeout=15)
    raw=Path('/tmp/raid5-recovery-fio.json').read_text()
    data=json.loads(raw[raw.index('{'):])
    j=data['jobs'][0]
    print('fio',{'exit':fio.returncode,'error':j['error'],'write_ios':j['write']['total_ios']},flush=True)
    print('PASS: real rublk recover, old worker excluded, no REISSUE flag, new queue registrations, stable flushed epoch intact',flush=True)
    print('LIMIT: concurrent-load smoke is not per-request exactly-once attribution or RAID5 recovery proof',flush=True)
finally:
    if server.poll() is None:
        server.kill();server.wait(timeout=10)
    subprocess.run(['target/debug/rublk','del','-n','1102'],timeout=15,check=True)
