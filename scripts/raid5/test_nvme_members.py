from pathlib import Path
import tempfile
import unittest
from nvme_members import resolve_members


class MemberIdentity(unittest.TestCase):
    def fixture(self, root, name, serial):
        p = root / name
        (p / "device").mkdir(parents=True)
        (p / "device/serial").write_text(serial)
        (p / "device/model").write_text("QEMU NVMe Ctrl")
        (p / "size").write_text(str(2147483648 // 512))

    def test_renumbered_workspace_is_never_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, serial in [("nvme0n1", "nvme-1"), ("nvme1n1", "nvme-2"),
                                 ("nvme2n1", "nvme-4"), ("nvme3n1", "nvme-3")]:
                self.fixture(root, name, serial)
            self.assertEqual(resolve_members(root), [Path("/dev/nvme0n1"),
                             Path("/dev/nvme1n1"), Path("/dev/nvme3n1")])

    def test_missing_member_does_not_substitute_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(4):
                self.fixture(root, f"nvme{i}n1", f"nvme-{i+1 if i<2 else i+2}")
            with self.assertRaisesRegex(RuntimeError, "nvme-3"):
                resolve_members(root)

    def test_duplicate_identity_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, serial in enumerate(["nvme-1", "nvme-2", "nvme-3", "nvme-3"]):
                self.fixture(root, f"nvme{i}n1", serial)
            with self.assertRaisesRegex(RuntimeError, "expected one namespace"):
                resolve_members(root)


if __name__ == "__main__":
    unittest.main()
