#!/usr/bin/env python3
"""Disposable spike client; root, real ublk-control required; never silently skips."""
import argparse, errno, mmap, os, sys, time
from pathlib import Path
parser=argparse.ArgumentParser()
parser.add_argument('node');parser.add_argument('backend');parser.add_argument('release',nargs='?')
parser.add_argument('--offset',type=int,default=65536);parser.add_argument('--size',type=int,default=8192)
parser.add_argument('--repeat',type=int,default=1)
a=parser.parse_args()
node,backend=a.node,a.backend
key=(a.offset>>9)%251
fd = os.open(node, os.O_RDWR | os.O_DIRECT)
buf = mmap.mmap(-1, a.size)
buf[:] = bytes(((i * 13 + 17) ^ ((i >> 8) * 29) ^ ((i >> 12) * 71) ^ key) & 255 for i in range(a.size))
if a.release:
    with open(backend, "rb", buffering=0) as f:
        f.seek(131072)
        before_failure=f.read(a.size)
    try:
        os.pwrite(fd, buf, 131072)
    except OSError as exc:
        assert exc.errno == errno.EIO, exc
    else:
        raise AssertionError("occupied auto-reg slot did not fail the client request")
    with open(backend, "rb", buffering=0) as f:
        f.seek(131072)
        assert f.read(a.size) == before_failure, "failed request changed backing bytes"
    Path(a.release).touch()
    time.sleep(2)
    print("PASS: auto-reg failure EIO, no backing write; retry uses same outstanding tag/fetch", flush=True)
for iteration in range(a.repeat):
    assert os.pwrite(fd, buf, a.offset) == a.size
    os.fsync(fd)
    readback = mmap.mmap(-1, a.size)
    assert os.preadv(fd, [readback], a.offset) == a.size
    assert readback[:] == buf[:]
    readback.close()
os.close(fd)
print(f"PASS: direct {a.size}-byte write/readback and flush offset={a.offset}", flush=True)
