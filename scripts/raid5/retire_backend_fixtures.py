#!/usr/bin/env python3
"""Retire only successfully verified, unmounted fixtures listed in our manifests."""
import hashlib,json,os
from pathlib import Path
assert os.geteuid()==0
root=Path(__file__).resolve().parents[2]/'docs/raid5/backend-faults';saved=0
for manifest in root.glob('*/manifest.json'):
 j=json.loads(manifest.read_text())
 for result in j['results']:
  if result.get('fixture_retired'):continue
  directory=Path(result['fixture']);assert directory.parent==Path('/home/xiao') and directory.name.startswith('raid5-backend-fault-');assert not os.path.ismount(directory/'mount')
  hashes={}
  for i in range(3):
   p=directory/'backing'/f'member{i}'
   if not p.exists():continue
   data=p.read_bytes();hashes[str(i)]=hashlib.sha256(data).hexdigest();(manifest.parent/f"{result['case']}-member{i}-metadata.bin").write_bytes(data[:12288]);saved+=p.stat().st_blocks*512;p.unlink()
  result['fixture_retired']=True;result['retired_fixture_sha256']=hashes
 tmp=manifest.with_suffix('.tmp');tmp.write_text(json.dumps(j,indent=2)+'\n');os.replace(tmp,manifest)
print('RETIRED_COMPLETED_FIXTURE_BYTES',saved)
