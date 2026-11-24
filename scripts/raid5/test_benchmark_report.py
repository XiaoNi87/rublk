import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BenchmarkReport(unittest.TestCase):
    def test_missing_latency_samples_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "one"
            out.mkdir()
            direction = dict(iops=0, bw=0, clat_ns={"N": 1, "bins": {}})
            (out / "metrics.json").write_text(json.dumps(dict(
                cell=0, target="rublk", repeat=0,
                before={"cpu_ticks": [0] * 10},
                after={"cpu_ticks": [1] * 10},
                fio={"jobs": [{"read": direction, "write": direction}]})))
            (root / "manifest.json").write_text(json.dumps(
                dict(results=["one"], cells=[{}], repeats=1)))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name(
                "benchmark_report.py")), str(root)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("histogram count does not match", result.stderr)
            self.assertFalse((root / "comparison.json").exists())

    def test_duplicate_paths_cannot_be_counted_as_independent_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps(
                dict(results=["same", "same"], cells=[], repeats=1)))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name(
                "benchmark_report.py")), str(root)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("duplicate result paths", result.stderr)
            self.assertFalse((root / "comparison.json").exists())

    def test_complete_collection_cannot_override_environment_exclusion(self):
        for kind in ["explicit", "kernel_alarm", "missing_logs"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                manifest = dict(results=[], cells=[], repeats=5, complete=True,
                                scope="fixture", pilot=False, binary_sha256="fixture")
                (root / "manifest.json").write_text(json.dumps(manifest))
                if kind != "missing_logs":
                    for stage in ["before", "after"]:
                        (root / f"kernel-{stage}.log").write_text("nvme queues ready\n")
                if kind == "explicit":
                    (root / "PERFORMANCE_INELIGIBLE.json").write_text(json.dumps(
                        dict(reason="NVMe abort in paired validation")))
                elif kind == "kernel_alarm":
                    (root / "kernel-after.log").write_text(
                        "nvme nvme0: FLUSH timeout, aborting\n")
                subprocess.run([sys.executable, str(Path(__file__).with_name(
                    "benchmark_report.py")), str(root)], check=True,
                    capture_output=True, text=True)
                report = json.loads((root / "comparison.json").read_text())
                self.assertTrue(report["data_collection_complete"])
                self.assertFalse(report["complete"])
                self.assertFalse(report["performance_eligible"])

    def test_repeat_dispersion_pooled_counts_and_counter_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = []
            cell = dict(rw="write", bs="128k", depth=16, jobs=4,
                        locality="disjoint", fsync=0)
            for target in ("rublk", "md"):
                for repeat in range(5):
                    name = f"{target}-{repeat}"
                    names.append(name)
                    out = root / name
                    out.mkdir()
                    # Pooled p99 stays in the fast bucket, but one whole repeat
                    # is slow. Averaging its p99 would invent a non-bucket result.
                    bins = {"1": 1000, "1004": 1} if repeat < 4 else {"1004": 10}
                    direction = dict(iops=10 + repeat, bw=40, bw_bytes=40960,
                                     clat_ns={"bins": bins})
                    empty = dict(iops=0, bw=0, bw_bytes=0, clat_ns={"bins": {}})
                    zero = [0] * 17
                    logical = zero.copy()
                    logical[2], logical[6] = 10, 100
                    member = zero.copy()
                    member[2], member[6] = 10, 150
                    before = dict(cpu_ticks=[0] * 10,
                                  logical_device=dict(name="logical", stat=zero),
                                  members={"member": zero})
                    after = dict(cpu_ticks=[10, 0, 10, 60, 5, 2, 3, 10, 5, 0],
                                 logical_device=dict(name="logical", stat=logical),
                                 members={"member": member})
                    (out / "metrics.json").write_text(json.dumps(dict(
                        cell=0, target=target, repeat=repeat, before=before,
                        after=after, fio={"jobs": [{"read": empty, "write": direction}]})))
            for stage in ["before", "after"]:
                (root / f"kernel-{stage}.log").write_text("nvme queues ready\n")
            manifest = dict(results=names, cells=[cell], repeats=5, complete=True,
                            scope="synthetic", pilot=False, binary_sha256="fixture")
            (root / "manifest.json").write_text(json.dumps(manifest))
            subprocess.run([sys.executable, str(Path(__file__).with_name(
                "benchmark_report.py")), str(root)], check=True,
                capture_output=True, text=True)
            report = json.loads((root / "comparison.json").read_text())
            self.assertTrue(report["complete"])
            target = report["cells"][0]["targets"]["rublk"]
            latency = target["completion_latency"]["write"]
            self.assertEqual(latency["pooled_p99_upper_ns"], 1)
            self.assertEqual(latency["repeat_p99_upper_ns"]["median"], 1)
            self.assertEqual(latency["repeat_p99_upper_ns"]["min"], 1)
            self.assertEqual(latency["repeat_p99_upper_ns"]["max"], 1007)
            self.assertEqual(target["host_busy_percent"]["median"], 25)
            self.assertEqual(target["vm_steal_percent"]["median"], 10)
            self.assertEqual(target["whole_cell_traffic"][0]
                             ["member_write_over_logical_write"], 1.5)
            self.assertIsNone(target["completion_latency"]["read"]
                              ["repeat_p99_upper_ns"])


if __name__ == "__main__":
    unittest.main()
