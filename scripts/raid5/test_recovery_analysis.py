"""Small local fixtures; no device access or benchmark workload."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

spec = importlib.util.spec_from_file_location(
    "collector", Path(__file__).with_name("recovery_collect.py"))
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
NS = 1_000_000_000


class RecoveryAnalysis(unittest.TestCase):
    def analyze(self, pause, target="rublk"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "fio.json").write_text(json.dumps({"jobs": [{"job_start": 0}]}))
            rows = []
            for sec in range(49):
                sync = dict(started_unix_ns=NS, scan_completed_unix_ns=36 * NS,
                            completed_unix_ns=40 * NS,
                            finalization_started_unix_ns=36 * NS,
                            frontier=min(sec, 36),
                            paused=pause and 12 <= sec < 24)
                rows.append(dict(observed_monotonic_ns=sec * NS,
                                 observed_unix_ns=sec * NS,
                                 md=dict(sync_action="idle" if sec >= 40 else
                                         "frozen" if pause and 12 <= sec < 24 else
                                         "resync"),
                                 status=dict(pid=10, sample_unix_ns=sec * NS,
                                             sync=sync)))
            # A complete histogram each second, with exact counts in one bucket.
            hist = [100] + [0] * 63
            (out / "job_clat_hist.1.log").write_text("".join(
                ",".join(map(str, [sec * 1000, 0, 4096, *hist])) + "\n"
                for sec in range(1, 49)))
            report = collector.analyze(out, target, 36 * 131072, "0", "mixed",
                                       0, 0, raw=rows)
            (out / "analysis-input.json").write_text(json.dumps(
                {"origin_unix": 0, "origin_mono": 0}))
            subprocess.run([sys.executable, str(Path(__file__).with_name(
                "recovery_window_report.py")), str(out)], check=True,
                capture_output=True, text=True)
            windows = json.loads((out / "window-report.json").read_text())
            for mode in windows["modes"]:
                for window in mode["windows"]:
                    for quantiles in window["pooled_quantiles"]:
                        self.assertEqual(quantiles["p99_upper_ns"], 1)
                        self.assertGreater(quantiles["operations"], 0)
            paused_mode = next(m for m in windows["modes"]
                               if m["mode"] == "paused-phase-only")
            self.assertEqual(paused_mode["available"], pause)
            return report, json.loads((out / "bin-eligibility.json").read_text())

    def test_pause_is_not_relabelled_active(self):
        report, bins = self.analyze(True)
        self.assertTrue(any(b["cohort"] == "PAUSED" for b in bins))
        self.assertFalse(any(b["cohort"] == "ACTIVE" and
                             b["start_ns"] < 24 * NS and
                             b["end_ns"] > 12 * NS for b in bins))
        for start, end in report["pure_10s_windows"]:
            self.assertTrue(end <= 12 * NS or start >= 24 * NS)
        self.assertLess(report["observed_active_duration_ns"], 35 * NS)
        self.assertEqual(report["source_durations"]["scan_elapsed_ns"], 35 * NS)
        paused = report["phase_only_paused_pure_10s_windows"]
        self.assertTrue(paused)
        self.assertTrue(all(start >= 12 * NS and end <= 24 * NS
                            for start, end in paused))

    def test_no_pause_preserves_active_finalization_and_post(self):
        report, bins = self.analyze(False)
        cohorts = {b["cohort"] for b in bins}
        self.assertTrue({"ACTIVE", "FINALIZING", "POST"} <= cohorts)
        self.assertNotIn("PAUSED", cohorts)
        self.assertTrue(report["pure_10s_windows"])
        self.assertEqual(report["paused_pure_10s_windows"], [])
        self.assertEqual(report["observed_active_duration_ns"], 34 * NS)

    def test_md_frozen_is_not_active_or_completed(self):
        report, bins = self.analyze(True, "md")
        self.assertTrue(any(b["cohort"] == "PAUSED" for b in bins))
        self.assertFalse(any(b["cohort"] in ("ACTIVE", "POST") and
                             b["start_ns"] < 24 * NS and
                             b["end_ns"] > 12 * NS for b in bins))
        self.assertTrue(any(b["cohort"] == "POST" for b in bins))
        self.assertTrue(report["phase_only_paused_pure_10s_windows"])


if __name__ == "__main__":
    unittest.main()
