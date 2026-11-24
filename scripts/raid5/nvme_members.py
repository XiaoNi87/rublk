"""Resolve the three authorized test members by identity, never enumeration order."""
from pathlib import Path
import re

MEMBER_SERIALS = ("nvme-1", "nvme-2", "nvme-3")


def resolve_members(sysfs_root=Path("/sys/class/block"), dev_root=Path("/dev")):
    matches = {serial: [] for serial in MEMBER_SERIALS}
    for entry in sysfs_root.iterdir():
        if not re.fullmatch(r"nvme\d+n\d+", entry.name):
            continue
        serial = (entry / "device/serial").read_text().strip()
        if serial in matches:
            model = (entry / "device/model").read_text().strip()
            if model != "QEMU NVMe Ctrl":
                raise RuntimeError(f"unexpected model for {serial}: {model}")
            if int((entry / "size").read_text()) * 512 != 2147483648:
                raise RuntimeError(f"unexpected capacity for {serial}")
            matches[serial].append(dev_root / entry.name)
    for serial, paths in matches.items():
        if len(paths) != 1:
            raise RuntimeError(f"expected one namespace for {serial}, found {paths}")
    return [matches[serial][0] for serial in MEMBER_SERIALS]
