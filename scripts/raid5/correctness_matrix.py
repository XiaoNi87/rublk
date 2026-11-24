#!/usr/bin/env python3
"""Plain-binary correctness, bounded linearizability histories, md differential.
Disposable NEW regular-file/loop fixtures only. No performance claims.
"""
import argparse, concurrent.futures, hashlib, json, mmap, os, random, shutil, subprocess, tempfile, threading, time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--md',action='store_true');a=p.parse_args()
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2];os.chdir(root)
binary=Path(os.environ['RAID5_BINARY']).resolve();C=65536;P=1048576;N=32;CAP=N*2*C
out=root/'docs/raid5/correctness-matrix'/str(time.time_ns());out.mkdir(parents=True)
results=[];node=Path('/dev/ublkb1100');assert not node.exists()
def run(cmd,**kw):
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=30,**kw)
 assert r.returncode==0,(cmd,r.returncode,r.stdout,r.stderr)
 return r
def payload(token):return hashlib.shake_256(str(token).encode()).digest(4096)
def io(fd,off,data=None,size=4096):
 b=mmap.mmap(-1,len(data) if data is not None else size)
 try:
  if data is not None:b[:]=data;assert os.pwrite(fd,b,off)==len(data);return None
  assert os.preadv(fd,[b],off)==size;return b[:]
 finally:b.close()
def linearizable(hist,initial):
 # Each read/write is atomic at one 4 KiB logical address. Real-time edges
 # include all completed-before-start relationships, not scheduler guesses.
 pred=[sum(1<<j for j,b in enumerate(hist) if b['end']<x['start']) for x in hist]
 full=(1<<len(hist))-1;bad=set()
 def visit(mask,value):
  if mask==full:return []
  key=(mask,value)
  if key in bad:return None
  for i,h in enumerate(hist):
   if mask>>i&1 or pred[i]&~mask:continue
   if h['op']=='read' and h['value']!=value:continue
   rest=visit(mask|1<<i,h['value'] if h['op']=='write' else value)
   if rest is not None:return [i]+rest
  bad.add(key);return None
 return visit(0,initial)
def workload(device,seed,concurrent=True):
 expected=bytearray(CAP);cases=[];rng=random.Random(seed);fd=os.open(device,os.O_RDWR|os.O_DIRECT)
 try:
  for off,size in [(0,2*C),(2*C,2*C),(4*C,2*C),(512,512),(C-512,1024),(2*C-512,1024),(6*C+8192,4096),(10*C+C-4096,12288),(16*C,1048576)]:
   data=hashlib.shake_256(f'{seed}:{off}:{size}'.encode()).digest(size);io(fd,off,data);expected[off:off+size]=data;cases.append([off,size])
  os.fsync(fd);sequential_sha256=hashlib.sha256(expected).hexdigest();histories=[]
  if concurrent:
   for repeat in range(3):
    # Hot shared address, disjoint blocks in same stripe, independent stripes.
    for mode in ['same-block','same-stripe','disjoint-stripes']:
     offsets=[24*2*C+(0 if mode=='same-block' else w*4096 if mode=='same-stripe' else w*2*C) for w in range(4)]
     initial={o:hashlib.sha256(expected[o:o+4096]).hexdigest() for o in offsets};history=[];lock=threading.Lock();barrier=threading.Barrier(4)
     def worker(w):
      allowed=sorted(os.sched_getaffinity(0));os.sched_setaffinity(0,{allowed[(2*w)%len(allowed)]})
      local=os.open(device,os.O_RDWR|os.O_DIRECT);barrier.wait()
      try:
       for k in range(6):
        write=(w+k)%3!=0;data=payload((seed,repeat,mode,w,k)) if write else None
        start=time.monotonic_ns();value=io(local,offsets[w],data);end=time.monotonic_ns()
        rec=dict(worker=w,cpu=next(iter(os.sched_getaffinity(0))),op='write' if write else 'read',offset=offsets[w],start=start,end=end,value=hashlib.sha256(data if write else value).hexdigest())
        with lock:history.append(rec)
      finally:os.close(local)
     with concurrent_futures.ThreadPoolExecutor(4) as pool:list(pool.map(worker,range(4)))
     orders={}
     for off in set(offsets):
      value=io(fd,off);start=time.monotonic_ns();history.append(dict(worker=-1,op='read',offset=off,start=start,end=time.monotonic_ns(),value=hashlib.sha256(value).hexdigest()))
      h=[x for x in history if x['offset']==off];order=linearizable(h,initial[off]);assert order is not None,('nonlinearizable',h)
      orders[str(off)]=order;expected[off:off+4096]=value
     histories.append(dict(repeat=repeat,mode=mode,history=history,serial_orders=orders))
  os.fsync(fd)
  for off in range(0,CAP,2*C):assert io(fd,off,size=2*C)==expected[off:off+2*C],('logical mismatch',off)
  return expected,dict(seed=seed,cases=cases,histories=histories,sequential_sha256=sequential_sha256,logical_sha256=hashlib.sha256(expected).hexdigest())
 finally:os.close(fd)
# Alias avoids parameter named concurrent shadowing module.
concurrent_futures=concurrent.futures

def physical(paths,offsets,expected):
 files=[x.read_bytes() for x in paths]
 for stripe in range(N):
  parity=2-stripe%3;d0=(parity+1)%3;d1=(parity+2)%3
  blocks=[files[m][offsets[m]+stripe*C:offsets[m]+(stripe+1)*C] for m in range(3)]
  assert blocks[d0]+blocks[d1]==expected[stripe*2*C:(stripe+1)*2*C],('physical ordering',stripe)
  assert bytes(x^y for x,y in zip(blocks[d0],blocks[d1]))==blocks[parity],('parity',stripe)
 return [hashlib.sha256(x).hexdigest() for x in files]
try:
 for q in [1,4]:
  for depth in [1,16,128]:
   directory=Path(tempfile.mkdtemp(prefix='raid5-correctness-',dir='/home/xiao'));paths=[directory/f'm{i}' for i in range(3)]
   for f in paths:
    with f.open('xb') as h:h.truncate(P+N*C)
   log=out/f'q{q}-d{depth}.log';server=None;ok=False
   try:
    with log.open('w') as h:
     server=subprocess.Popen([str(binary),'add','raid5','-z','--test-files','--foreground','-n','1100','-q',str(q),'-d',str(depth),*map(str,paths)],stdout=h,stderr=h)
    for _ in range(2000):
     assert server.poll() is None,log.read_text()
     if node.exists():
      try:s=json.loads(Path('/run/rublk-raid5/raid5-1100.status.json').read_text())
      except (FileNotFoundError,ValueError):s={}
      if s.get('pid')==server.pid and s.get('state')=='RUNNING':break
     time.sleep(.01)
    else:raise AssertionError('startup timeout')
    expected,result=workload(node,150016)
    run([str(binary),'del','-n','1100']);assert server.wait(timeout=30)==0
    result.update(queues=q,depth=depth,physical_sha256=physical(paths,[P]*3,expected))
    # Everything outside superblocks and used data must stay untouched.
    assert all(f.read_bytes()[12288:P]==bytes(P-12288) for f in paths)
    (out/f'q{q}-d{depth}-history.json').write_text(json.dumps(result,indent=2));results.append(dict(queues=q,depth=depth,passed=True,histories=9));ok=True;print('PASS',q,depth,flush=True)
   finally:
    if node.exists():run([str(binary),'del','-n','1100'])
    if server is not None and server.poll() is None:server.wait(timeout=30)
    if ok:shutil.rmtree(directory)
 if a.md:
  directory=Path(tempfile.mkdtemp(prefix='raid5-md-correctness-',dir='/home/xiao'));paths=[directory/f'm{i}' for i in range(3)];loops=[];loop_servers=[];md='/dev/md/raid5-correctness';active=False;ok=False
  try:
   for f in paths:
    with f.open('xb') as h:h.truncate(17*1048576)
    number=1101+len(loops);device=f'/dev/ublkb{number}';assert not Path(device).exists()
    h=(out/f'md-loop-{number}.log').open('w')
    proc=subprocess.Popen([str(binary),'add','loop','--foreground','-n',str(number),'--logical-block-size','512','--physical-block-size','4096','-f',str(f)],stdout=h,stderr=h);loop_servers.append((number,proc,h))
    for _ in range(1000):
     assert proc.poll() is None
     if Path(device).exists():
      state=run([str(binary),'list','-n',str(number)]).stdout
      if 'state LIVE' in state:break
     time.sleep(.01)
    else:raise AssertionError('ublk loop startup timeout')
    loops.append(device)
   run(['/usr/sbin/mdadm','--create',md,'--run','--metadata=1.2','--level=5','--raid-devices=3','--chunk=64','--layout=left-symmetric','--data-offset=1M','--size=2048','--assume-clean',*loops]);active=True
   expected,result=workload(md,150016,False)
   target_hashes={json.loads(p.read_text())['sequential_sha256'] for p in out.glob('q*-history.json')}
   assert target_hashes=={result['sequential_sha256']},'md/target sequential content differs'
   result['rublk_matrix_sequential_sha256']=sorted(target_hashes)
   run(['/usr/sbin/mdadm','--stop',md]);active=False
   offsets=[];examine=[]
   for loop in loops:
    text=run(['/usr/sbin/mdadm','--examine',loop]).stdout;examine.append(text)
    import re
    offsets.append(int(re.search(r'Data Offset\s*:\s*(\d+) sectors',text)[1])*512)
   result.update(data_offsets=offsets,examine=examine,physical_sha256=physical(paths,offsets,expected),scope='same deterministic healthy sequential workload; not timing or concurrent-history equivalence')
   (out/'md-differential.json').write_text(json.dumps(result,indent=2));results.append(dict(md=True,passed=True));ok=True;print('PASS md differential',flush=True)
  finally:
   if active:run(['/usr/sbin/mdadm','--stop',md])
   for number,proc,h in loop_servers:
    run([str(binary),'del','-n',str(number)]);assert proc.wait(timeout=30)==0;h.close()
   if ok:shutil.rmtree(directory)
finally:
 (out/'manifest.json').write_text(json.dumps(dict(binary=str(binary),binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),results=results,complete=len(results)==(7 if a.md else 6)),indent=2));print(out,flush=True)
