import errno,mmap,os,sys,time
from pathlib import Path
node,member,release=map(Path,sys.argv[1:]);fd=os.open(node,os.O_RDWR|os.O_DIRECT);buf=mmap.mmap(-1,4096);buf[:]=bytes([165])*4096
try:os.pwrite(fd,buf,0)
except OSError as e:assert e.errno==errno.EIO,e
else:raise AssertionError('occupied kernel auto-registration did not fail')
with member.open('rb',buffering=0) as f:f.seek(1048576);assert f.read(4096)==bytes(4096),'failed BIO reached backend'
release.touch()
for _ in range(1000):
 if Path(str(release)+'.done').exists():break
 time.sleep(.01)
else:raise AssertionError('registration occupation not released')
buf[:]=bytes([41])*4096;assert os.pwrite(fd,buf,0)==4096;os.fsync(fd);read=mmap.mmap(-1,4096);assert os.preadv(fd,[read],0)==4096 and read[:]==buf[:]
os.close(fd);read.close();buf.close();print('PASS kernel-only registration failure: one EIO/no backend mutation, then target tag progress restored')
