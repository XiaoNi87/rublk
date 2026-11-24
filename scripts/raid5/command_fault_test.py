#!/usr/bin/env python3
"""Real command-CQE rejection, externally injected before kernel submission."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--expect-replay", action="store_true")
parser.add_argument("--hold-backend", action="store_true")
parser.add_argument("--hold-peer", action="store_true")
parser.add_argument("--fetch", choices=["single", "all"])
parser.add_argument("--queues", type=int, default=1)
parser.add_argument("--depth", type=int, default=1)
args = parser.parse_args()
device_id = os.environ.get("RAID5_FAULT_DEVICE", "1101")
assert os.geteuid() == 0
root = Path(__file__).resolve().parents[2]
os.chdir(root)
binary = Path(os.environ.get("RAID5_BINARY", json.loads(
    (root / "docs/raid5/binaries/current.json").read_text())["binary"])).resolve()
assert not Path("/dev/ublkb" + device_id).exists()
out = root / "docs/raid5/command-faults" / str(time.time_ns())
out.mkdir(parents=True)
work = Path(tempfile.mkdtemp(prefix="raid5-command-fault-", dir="/home/xiao"))
paths = [work / f"member{i}" for i in range(3)]
for path in paths:
    with path.open("xb") as f:
        f.truncate(2 * 1048576)
fs = None
fslog = None
mount = work / "mount"
control = work / "control"
if args.hold_backend:
    assert args.queues >= 2 and not args.hold_peer
    flags = subprocess.check_output(["pkg-config","--cflags","--libs","fuse3"],text=True).split()
    subprocess.run(["clang-19","-O2","-Wall","-Wextra","-Werror",str(root/"scripts/raid5/fault_fs.c"),
        "-o",str(root/"target/raid5-fault-fs"),*flags],check=True)
    mount.mkdir();fslog=(out/"fuse.log").open("w")
    fs=subprocess.Popen([str(root/"target/raid5-fault-fs"),str(mount),str(work),str(control)],
        env={**os.environ,"RAID5_FAULT_FS_MULTITHREAD":"1"},stdout=fslog,stderr=fslog)
    deadline=time.monotonic()+5
    while not os.path.ismount(mount):
        assert fs.poll() is None and time.monotonic()<deadline
        time.sleep(.01)
    paths=[mount/f"member{i}" for i in range(3)]
log = (out / "server.log").open("w")
env = os.environ.copy()
if args.hold_backend:
    env["RAID5_FAULT_ANY_TAG"]="1"
if args.hold_peer:
    assert args.queues >= 2
    env["RAID5_FAULT_HOLD"] = "1"
if args.fetch:
    env["RAID5_FAULT_FETCH"] = "1"
    if args.fetch == "all":
        env["RAID5_FAULT_FETCH_ALL"] = "1"
maps_before = {m["id"] for m in json.loads(subprocess.check_output(["bpftool", "-j", "map", "show"]))}
server = subprocess.Popen([str(root / "target/raid5-command-fault"),
    str(out / "child.pid"), str(binary), "add", "raid5", "-z", "--test-files",
    "--foreground", "-n", device_id, "-q", str(args.queues), "-d", str(args.depth), *map(str, paths)],
    env=env,
    stdout=log, stderr=log, start_new_session=True)
client = None
peer = None
pid = None
def wait_deleted():
    deadline=time.monotonic()+10
    while Path("/sys/class/ublk-char/ublkc"+device_id).exists():
        assert time.monotonic()<deadline, "kernel device deletion timeout"
        time.sleep(.01)
def verify_no_maps():
    deadline = time.monotonic() + 5
    while True:
        extra = {m["id"] for m in json.loads(subprocess.check_output(["bpftool", "-j", "map", "show"]))} - maps_before
        if not extra: return
        assert time.monotonic() < deadline, ("leaked BPF maps",extra)
        time.sleep(.02)
def save_dirty_slots():
    for i,path in enumerate(paths):
        (out / f"dirty-member{i}.bin").write_bytes(path.read_bytes()[:8192])
def retry():
    retry_log = (out / "retry.log").open("w")
    process = subprocess.Popen([str(binary), "add", "raid5", "-z", "--test-files",
        "--foreground", "--assemble", "-n", device_id, "-q", str(args.queues),
        "-d", str(args.depth), *map(str,paths)], stdout=retry_log, stderr=retry_log)
    deadline = time.monotonic() + 15
    while True:
        assert process.poll() is None, (out / "retry.log").read_text()
        try:
            status = json.loads(Path("/run/rublk-raid5/raid5-"+device_id+".status.json").read_text())
        except FileNotFoundError: status = {}
        if status.get("pid") == process.pid and status.get("state") == "RUNNING": break
        assert time.monotonic() < deadline, status
        time.sleep(.01)
    import mmap
    data = mmap.mmap(-1,4096)
    fd = os.open("/dev/ublkb"+device_id,os.O_RDWR|os.O_DIRECT)
    assert os.preadv(fd,[data],0)==4096
    assert data[:] == bytes([0 if args.fetch else 165])*4096
    data[:] = bytes([90])*4096
    assert os.pwrite(fd,data,4096)==4096
    os.fsync(fd)
    data[:] = bytes(4096)
    assert os.preadv(fd,[data],4096)==4096 and data[:]==bytes([90])*4096
    os.close(fd);data.close()
    subprocess.run([str(binary),"del","-n",device_id],check=True,capture_output=True,timeout=15)
    process.wait(timeout=15);retry_log.close()
    wait_deleted()
    assert process.returncode == 0, (out / "retry.log").read_text()
    for path in paths:
        raw = path.read_bytes()[:8192]
        assert all(int.from_bytes(raw[o+20:o+24],"little")==1 for o in (0,4096))
    files = [p.read_bytes() for p in paths]
    for stripe in range(16):
        parity=2-stripe%3;a,b=(parity+1)%3,(parity+2)%3;off=1048576+stripe*65536
        assert bytes(x^y for x,y in zip(files[a][off:off+65536],files[b][off:off+65536]))==files[parity][off:off+65536]
    verify_no_maps()

try:
    if args.fetch:
        server.wait(timeout=30)
        log.flush()
        content = (out / "server.log").read_text()
        assert server.returncode != 0 and "NEGATIVE_COMMAND_CQE res=-22" in content, content
        expected = args.queues * args.depth if args.fetch == "all" else 1
        match = re.search(r"COMMAND_FAULT_SUMMARY injected=(\d+) negative_seen=(\d+)", content)
        assert match and tuple(map(int,match.groups())) == (expected,expected), content
        assert "total_writes=0" in content, content
        assert not Path("/dev/ublkb" + device_id).exists(), content
        for path in paths:
            raw = path.read_bytes()[:8192]
            assert all(int.from_bytes(raw[o+20:o+24], "little") == 2 for o in (0,4096))
        save_dirty_slots()
        wait_deleted()
        verify_no_maps()
        retry()
        (out / "manifest.json").write_text(json.dumps(dict(complete=True, retry="full sync, read/write/flush, physical parity and clean stop passed",
            mode=args.fetch, queues=args.queues, depth=args.depth,
            binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            child_exit=server.returncode, timeout_seconds=30, backend_write_sqe_attempts=0,
            metadata="all six slots DIRTY", device_published=False), indent=2)+"\n")
        print("PASS FETCH", out)
        raise SystemExit(0)
    for _ in range(3000):
        assert server.poll() is None, (out / "server.log").read_text()
        try:
            pid = int((out / "child.pid").read_text())
            status = json.loads(Path("/run/rublk-raid5/raid5-" + device_id + ".status.json").read_text())
        except (ValueError, FileNotFoundError):
            status = {}
        if status.get("pid") == pid and status.get("state") == "RUNNING":
            break
        time.sleep(.01)
    else:
        raise AssertionError("traced server did not become RUNNING")
    deadline = time.monotonic() + 5
    while not Path("/dev/ublkb" + device_id).exists():
        assert time.monotonic() < deadline, "own device node timeout"
        time.sleep(.01)
    code = """import mmap,os
buf=mmap.mmap(-1,4096);buf[:]=bytes([165])*4096
fd=os.open('/dev/ublkbDEVICE_ID',os.O_RDWR|os.O_DIRECT)
try:
 n=os.pwrite(fd,buf,0);assert n==4096;print('WRITE_SUCCESS')
except OSError as e: print('WRITE_ERROR',e.errno)
finally:os.close(fd);buf.close()
"""
    code = code.replace("DEVICE_ID", device_id)
    prefix = []
    if args.hold_peer or args.hold_backend:
        content = (out / "server.log").read_text()
        cpus = dict((int(q), int(c)) for q,c in re.findall(r"queue (\d+) tid: \d+ affinity\((\d+)\)",content))
        deadline = time.monotonic() + 5
        while not (0 in cpus and 1 in cpus):
            assert time.monotonic() < deadline, content
            time.sleep(.01)
            content = (out / "server.log").read_text()
            cpus = dict((int(q), int(c)) for q,c in re.findall(r"queue (\d+) tid: \d+ affinity\((\d+)\)",content))
        if args.hold_backend:
            control.write_text("2 write 1114112 hold-success\n")
        peer_code = code.replace("os.pwrite(fd,buf,0)", "os.pwrite(fd,buf,131072)")
        peer = subprocess.Popen(["taskset", "-c", str(cpus[1]), "python3", "-c", peer_code],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 10
        while not (Path(str(control)+".hit").exists() if args.hold_backend else
                   "HOLD_ORIGINAL_BACKEND_CQE" in (out / "server.log").read_text()):
            assert time.monotonic() < deadline, (out / "server.log").read_text()
            time.sleep(.01)
        prefix = ["taskset", "-c", str(cpus[0])]
    client = subprocess.Popen([*prefix, "python3", "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    if args.hold_backend:
        deadline=time.monotonic()+10
        while "NEGATIVE_COMMAND_CQE" not in (out/"server.log").read_text():
            assert time.monotonic()<deadline,(out/"server.log").read_text()
            time.sleep(.01)
        time.sleep(.2)
        assert peer.poll() is None and server.poll() is None, "in-flight backend owner released early"
        # The FUSE write callback has not returned: original backend CQE cannot
        # exist yet, so target/arena/permit must remain owned throughout stop.
        assert Path(f"/proc/{pid}/exe").exists()
        (out/"pending-backend-proof.json").write_text(json.dumps(dict(
            held_callback=True,original_cqe_unavailable=True,server_alive_after_error=True,
            peer_blocked_after_error=True,hold_after_error_seconds=.2),indent=2)+"\n")
        Path(str(control)+".release").touch()
    text = client.communicate(timeout=30)[0]
    (out / "client.log").write_text(text)
    if args.hold_peer or args.hold_backend:
        peer_text = peer.communicate(timeout=30)[0]
        assert peer.returncode == 0 and "WRITE_ERROR 5" in peer_text, peer_text
        (out / "peer-client.log").write_text(peer_text)
    assert client.returncode == 0, text
    if args.expect_replay:
        assert "WRITE_SUCCESS" in text, text
        subprocess.run([str(binary), "del", "-n", device_id], check=True,
                       capture_output=True, timeout=30)
    server.wait(timeout=30)
    log.close()
    content = (out / "server.log").read_text()
    match = re.search(r"COMMAND_FAULT_SUMMARY injected=(\d+) negative_seen=(\d+) "
        r"writes_before=(\d+) writes_after=(\d+) total_writes=(\d+) child_exit=(\d+)", content)
    assert match, content
    injected, negative, before, after, total, exit_code = map(int, match.groups())
    assert injected == negative == 1, content
    if args.hold_peer:
        assert "BACKEND_HOLD_SUMMARY held=1 released=1 drained=1" in content, content
    assert before == 2, content
    if args.expect_replay:
        assert after >= 2 and exit_code == 0, content
    else:
        assert "WRITE_ERROR 5" in text and after == 0 and exit_code != 0, (text, content)
        for path in paths:
            raw = path.read_bytes()[:8192]
            assert all(int.from_bytes(raw[o+20:o+24], "little") == 2 for o in (0,4096))
    if Path("/dev/ublkb" + device_id).exists():
        subprocess.run([str(binary), "del", "-n", device_id], check=True,
                       capture_output=True, timeout=30)
    files = [p.read_bytes() for p in paths]
    for stripe in range(16):
        parity = 2 - stripe % 3
        a, b = (parity + 1) % 3, (parity + 2) % 3
        off = 1048576 + stripe * 65536
        assert bytes(x ^ y for x,y in zip(files[a][off:off+65536],
                     files[b][off:off+65536])) == files[parity][off:off+65536]
    save_dirty_slots()
    wait_deleted()
    verify_no_maps()
    if not args.expect_replay:
        retry()
    manifest = dict(complete=True, retry=not args.expect_replay, binary=str(binary),
        binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        pending_backend=args.hold_backend, held_peer=args.hold_peer, queues=args.queues, depth=args.depth,
        expected_old_replay=args.expect_replay, real_command_cqe=-22,
        backend_write_sqe_attempts_before=before, backend_write_sqe_attempts_after=after,
        client=text.strip(), child_exit=exit_code, physical_stripes_verified=16,
        fixture=str(work), scope="external ptrace SQE mutation; no production fault hook")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("PASS", out, manifest)
finally:
    # Release a real backend before attempting deletion, including failed tests.
    if args.hold_backend:
        Path(str(control)+".release").touch()
    cleanup_errors=[]
    for process in (client, peer):
        if process is not None and process.poll() is None:
            process.kill()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: cleanup_errors.append("client did not exit")
    if Path("/sys/class/ublk-char/ublkc"+device_id).exists():
        try:
            subprocess.run([str(binary),"del","-n",device_id],capture_output=True,timeout=10)
        except subprocess.TimeoutExpired: cleanup_errors.append("device deletion timeout")
    if server.poll() is None:
        try: server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(server.pid,signal.SIGKILL)
            try: server.wait(timeout=5)
            except subprocess.TimeoutExpired: cleanup_errors.append("traced server did not exit")
    if not log.closed: log.close()
    if fs is not None:
        subprocess.run(["fusermount3","-u",str(mount)],capture_output=True,timeout=10)
        fs.wait(timeout=10);fslog.close()
    if cleanup_errors:
        (out/"cleanup-errors.json").write_text(json.dumps(cleanup_errors)+"\n")
        raise RuntimeError(cleanup_errors)
