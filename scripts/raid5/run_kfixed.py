#!/usr/bin/env python3
"""Run the disposable real-device gate; cleanup only its reserved device ID."""
import json, os, re, shlex, subprocess, sys, time
from pathlib import Path
root=Path(__file__).resolve().parents[2]
binary=Path(os.environ.get('RAID5_BINARY',str(root/'target/debug/rublk'))).resolve()
fixture=json.loads((root/'docs/raid5/fixtures.json').read_text())['members'][0]
node=Path('/dev/ublkb1100')
assert not node.exists(), 'reserved ID 1100 is already occupied'
failure='--failure' in sys.argv
copy_control='--copy-control' in sys.argv
assert not (failure and copy_control)
release=Path(fixture).parent/'release-occupied'
release.unlink(missing_ok=True)
logpath=root/'docs/raid5'/('copy-control-server.log' if copy_control else 'kfixed-failure.log' if failure else 'kfixed-mixed.log')
log=logpath.open('w')
cmd=['sudo','-n']
if failure:cmd+=['env','RAID5_SPIKE_RELEASE_FILE='+str(release)]
if copy_control:
    cmd += [str(binary),'add','loop','-n','1100','--foreground','-a','-f',fixture]
else:
    cmd += [str(root/'target/debug/examples/raid5_kfixed_probe'),'1100',fixture]
server=subprocess.Popen(cmd,stdout=log,stderr=log,start_new_session=True)
try:
    for _ in range(100):
        if 'READY 1100' in logpath.read_text() or (copy_control and node.exists()):break
        if server.poll() is not None:raise RuntimeError(logpath.read_text())
        time.sleep(.05)
    assert node.exists() and (copy_control or 'READY 1100' in logpath.read_text()), logpath.read_text()
    client=['python3',str(root/'scripts/raid5/kfixed_client.py'),str(node),fixture]
    if failure:client+=[str(release)]
    trace=subprocess.run(['sudo','-n','bpftrace',str(root/'scripts/raid5/kfixed_trace.bt'),'-c',shlex.join(client)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=30)
    tracepath=root/'docs/raid5'/('copy-control-trace.log' if copy_control else 'kfixed-failure-trace.log' if failure else 'kfixed-mixed-trace.log')
    tracepath.write_text(trace.stdout)
    print(trace.stdout,flush=True)
    assert trace.returncode==0, trace.returncode
    counts={k:int(v) for k,v in re.findall(r'@(bpf|copy|regfail|regok|user|kfixed|destroy): (\d+)',trace.stdout)}
    if copy_control:
        assert counts['copy']>0 and counts['bpf']==0,counts
    else:
        assert counts['bpf']>=2 and counts['copy']==0 and counts['regok']>0,counts
        assert counts['user']>=2 and counts['kfixed']>=2,counts
        assert counts['destroy']==counts['user']+counts['kfixed'],counts
        if failure:assert counts['regfail']>=1,counts
        assert 'independent parity verified' in logpath.read_text(),logpath.read_text()
    print(logpath.read_text(),flush=True)
finally:
    if failure and not release.exists():release.touch()
    r=subprocess.run(['sudo','-n','timeout','5',str(binary),'del','-n','1100'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    if r.returncode:
        # Failure cleanup: kill precisely this spike, wait for kernel teardown.
        subprocess.run(['sudo','-n','pkill','-KILL','-f',r'^'+re.escape(str(root/'target/debug/examples/raid5_kfixed_probe'))+' 1100 '])
    server.wait(timeout=10)
    for _ in range(50):
        if not node.exists():break
        time.sleep(.1)
    assert not node.exists(), 'test device cleanup failed'

assert server.returncode==0,logpath.read_text()
if not copy_control:assert 'DRAINED qid=0' in logpath.read_text(),logpath.read_text()
