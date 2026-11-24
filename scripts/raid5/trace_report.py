#!/usr/bin/env python3
"""Bounded PID/thread/ring/tag/range correlation. Never unique-BIO proof."""
import argparse,hashlib,json,re
from pathlib import Path
root=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--trace',type=Path,default=root/'docs/raid5/target-correlated-trace.log');p.add_argument('--control',type=Path,default=root/'docs/raid5/final-copy-control.log');p.add_argument('--sessions',type=Path,default=root/'docs/raid5');p.add_argument('--binary',type=Path,default=root/'target/release/rublk');args=p.parse_args();s=args.trace.read_text()
def counts(text):return {k:int(v) for k,v in re.findall(r'^@(bpf|copy|destroy|kfixed|user|regfail|regok): (\d+)$',text,re.M)}
def tuples(name):return [tuple(map(int,x.split(', '))) for x in re.findall(r'^@'+name+r'\[([^]]+)\]:',s,re.M)]
def entries(name):return [(tuple(map(int,x.split(', '))),int(v)) for x,v in re.findall(r'^@'+name+r'\[([^]]+)\]: (\d+)$',s,re.M)]
c=counts(s);assert c['copy']==0 and c['regfail']==0 and c['bpf']>0 and c['kfixed']>0 and c['user']>0,c
control=counts(args.control.read_text());assert control['copy']>0 and control['bpf']==0,control
assert 'PASS retained ublk recover:' in s and 'PASS full sync concurrent' in s,'client/physical oracle did not complete'
normal={};known_pids=set()
for path in args.sessions.glob('target-integration-session-*.log'):
 text=path.read_text();m=re.search(r'daemon pid (\d+)',text)
 if not m:continue
 pid=int(m[1]);known_pids.add(pid)
 for qid,tid in re.findall(r'queue (\d+) tid: (\d+)',text):normal[pid,int(tid)]=int(qid)
fixed=tuples('fixed_samples');desc=tuples('iterator_samples');ordinary=tuples('ordinary_samples');owners=tuples('ring_owners');registration=tuples('registration_samples');opcodes=dict(entries('ring_opcodes'))
roles={}
for pid,tid,ctx in owners:
 if (pid,tid) in normal:role=f'normal_queue_{normal[pid,tid]}'
 elif pid==tid:role='controller_or_startup'
 elif opcodes.get((pid,tid,ctx,46),0):role='normal_queue_recovered_no_printed_qid'
 elif opcodes.get((pid,tid,ctx,65),0):role='background_sync'
 else:role='control_or_unclassified'
 roles[pid,tid,ctx]=role
assert all((pid,tid,ctx) in roles for pid,tid,ctx,*_ in fixed),'fixed owner absent'
summary=[]
for (pid,tid,ctx),role in sorted(roles.items()):
 fs=[x for x in fixed if x[:3]==(pid,tid,ctx)];ds=[x for x in desc if x[:3]==(pid,tid,ctx)];ordinary_ring=[x for x in ordinary if x[:3]==(pid,tid,ctx)]
 summary.append(dict(pid=pid,tid=tid,ctx=ctx,role=role,opcodes={str(op):n for (owner,thread,ring,op),n in opcodes.items() if (owner,thread,ring)==(pid,tid,ctx)},fixed_samples=len(fs),user_iterator_samples=sum(x[3]==0 for x in ds),kfixed_iterator_samples=sum(x[3]==3 for x in ds),ordinary_read_samples=sum(x[3]==22 for x in ordinary_ring),arena_write_samples=sum(x[3]==23 for x in ordinary_ring)))
normal_rings=[r for r in summary if r['role'].startswith('normal_queue')];sync_rings=[r for r in summary if r['role']=='background_sync'];assert len(normal_rings)>=2 and sync_rings
assert any(r['kfixed_iterator_samples'] and r['fixed_samples'] and r['user_iterator_samples'] and r['arena_write_samples'] for r in normal_rings),'normal RMW missing one memory class'
assert any(r['user_iterator_samples'] and r['ordinary_read_samples'] and r['arena_write_samples'] for r in sync_rings),'background paths absent'
assert all(r['kfixed_iterator_samples']==0 and r['fixed_samples']==0 for r in sync_rings),'background unexpectedly used request KFIXED'
a={(pid,tid,ctx,tag,addr,size) for pid,tid,ctx,op,tag,addr,size,off in fixed if op==5};b={(pid,tid,ctx,tag,addr,size) for pid,tid,ctx,kind,tag,addr,size in desc if kind==3};matched=sorted(a&b);assert matched and any(x[4]>0 for x in matched)
rotations=sorted({((off-1048576)//65536)%3 for pid,tid,ctx,op,tag,addr,size,off in fixed if op==5 and off>=1048576});assert rotations==[0,1,2],rotations
assert any(op==4 for pid,tid,ctx,op,*_ in fixed),'ReadFixed not sampled'
user_read={(pid,tid,ctx,addr,size) for pid,tid,ctx,kind,tag,addr,size in desc if kind==0}&{(pid,tid,ctx,addr,size) for pid,tid,ctx,op,addr,size,off in ordinary if op==22};assert user_read
# USER source slots are separated by one64KiB chunk; the output slot is
# the following chunk. Match those addresses with ordinary parity Write.
users={(pid,tid,ctx,addr,size) for pid,tid,ctx,kind,tag,addr,size in desc if kind==0}
writes={(pid,tid,ctx,addr,size) for pid,tid,ctx,op,addr,size,off in ordinary if op==23}
arena_matches=sorted((pid,tid,ctx,addr,size) for pid,tid,ctx,addr,size in users if (pid,tid,ctx,addr+65536,size) in users and (pid,tid,ctx,addr+131072,size) in writes)
assert any(roles[x[:3]].startswith('normal_queue') for x in arena_matches),'normal arena output address absent'
assert any(roles[x[:3]]=='background_sync' for x in arena_matches),'sync arena output address absent'
registered=[]
for pid,tid,ctx,tag,addr,size in matched:
 candidates=[(rq,sector,length) for p,t,rq,index,sector,length in registration if p==pid and t==tid and index==tag and addr+size<=length]
 if candidates:registered.append(dict(pid=pid,tid=tid,ring=ctx,index=tag,original_offset=addr,length=size,request_candidates=candidates))
assert registered,'no kernel-registration/fixed/BPF range association'
report=dict(counts=c,copy_positive_control=control,ring_roles=summary,fixed_samples=len(fixed),iterator_samples=len(desc),ordinary_samples=len(ordinary),registration_samples=len(registration),matched_ring_tag_range_writes=len(matched),nonzero_original_offsets=sum(x[4]>0 for x in matched),parity_rotations=rotations,user_readback_address_matches=len(user_read),arena_output_address_matches=arena_matches,registration_associations=registered,sample_keys=matched,binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),trace_sha256=hashlib.sha256(args.trace.read_bytes()).hexdigest(),scope='sampled integrated request pages use KFIXED; separate USER readback + ordinary arena Write; background sync isolated by owner/ring; positive-copy control',limits=['at most64 samples per process/ring and64 registration samples per process; not exhaustive','request pointers/tags/ring addresses can be reused; association candidates are not unique BIO identities','controller/startup traffic reported separately; no universal absence-of-copy proof','bpf_issue_wall_ns is elapsed latency, not CPU execution time','global copy probe coverage is only ublk_copy_user_pages.isra.0 on this pinned kernel','physical NVMe and real power-loss acceptance not performed'])
args.trace.with_suffix('.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(dict(counts=c,normal_rings=len(normal_rings),background_rings=len(sync_rings),range_matches=len(matched),registration_associations=len(registered),user_readback_matches=len(user_read),positive_control=control),indent=2))
