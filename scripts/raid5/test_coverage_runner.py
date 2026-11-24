"""Exercise only the draft runner helpers, never execute collection commands."""
import ast
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock
import os


class CoverageRunner(unittest.TestCase):
    def run_case(self, exit_code):
        source = Path(__file__).with_name("coverage.py").read_text()
        tree = ast.parse(source)
        helpers = ast.Module(body=[node for node in tree.body
                                   if isinstance(node, ast.FunctionDef) and
                                   node.name in ("save", "run")], type_ignores=[])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            raw = out / "raw"
            raw.mkdir()
            mock = Mock(return_value=subprocess.CompletedProcess([], exit_code, "OUT", "ERR"))
            context = dict(out=out, raw=raw, env={"LLVM_PROFILE_FILE": "old"},
                           runs=[], time=time, os=os, json=json,
                           subprocess=type("Subprocess", (), {"run": staticmethod(mock)}))
            exec(compile(helpers, "coverage_helpers", "exec"), context)
            command = ["sudo", "-n", "env", "LLVM_PROFILE_FILE=old", "worker"]
            if exit_code:
                with self.assertRaises(AssertionError):
                    context["run"]("real-target", command)
            else:
                context["run"]("real-target", command)
            pattern = str(raw / "real-target" / "%p-%m.profraw")
            args, kwargs = mock.call_args
            self.assertIn("LLVM_PROFILE_FILE=" + pattern, args[0])
            self.assertEqual(kwargs["env"]["LLVM_PROFILE_FILE"], pattern)
            self.assertEqual(context["env"]["LLVM_PROFILE_FILE"], "old")
            self.assertEqual(command[3], "LLVM_PROFILE_FILE=old")
            manifest = json.loads((out / "runs.json").read_text())
            self.assertEqual(manifest[0]["exit"], exit_code)
            self.assertEqual((out / "real-target.log").read_text(), "OUTERR")

    def test_root_child_receives_suite_specific_profile_path(self):
        self.run_case(0)

    def test_failed_suite_keeps_log_and_manifest(self):
        self.run_case(1)


if __name__ == "__main__":
    unittest.main()
