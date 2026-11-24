#!/usr/bin/env python3
"""Fingerprint the exact source, including untracked target/vendor files."""
import hashlib,json,os,subprocess,tarfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def sources():
 paths=[]
 for name in ['Cargo.toml','Cargo.lock','build.rs','RAID5_DESIGN.md','src','tests','vendor','examples','scripts/raid5']:
  p=ROOT/name
  paths.extend(p.rglob('*') if p.is_dir() else [p])
 return sorted({p for p in paths if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'})
def fingerprint():return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources()}
if __name__=='__main__':
 os.chdir(ROOT);out=ROOT/'docs/raid5';paths=sources();archive=out/'source-snapshot.tar.gz'
 with tarfile.open(archive.with_suffix('.tmp'),'w:gz') as tar:
  for p in paths:tar.add(p,arcname=str(p.relative_to(ROOT)),recursive=False)
 os.replace(archive.with_suffix('.tmp'),archive)
 manifest={'captured_unix_ns':time.time_ns(),'base':subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip(),'branch':subprocess.run(['git','branch','--show-current'],capture_output=True,text=True,check=True).stdout.strip(),'source_sha256':fingerprint(),'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'committed':False}
 tmp=out/'source-manifest.tmp';tmp.write_text(json.dumps(manifest,indent=2)+'\n');os.replace(tmp,out/'source-manifest.json')
 print('PASS exact source manifest',len(paths))
