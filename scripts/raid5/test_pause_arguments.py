"""Validate the actual draft parser without opening devices or starting fio."""
import argparse
import ast
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


def parse(arguments):
    tree = ast.parse(Path(__file__).with_name("benchmark.py").read_text())
    nodes = []
    started = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "parser" for t in node.targets):
            started = True
        if not started:
            continue
        if isinstance(node, ast.Assert):
            break
        nodes.append(node)
    context = {"argparse": argparse}
    with patch.object(sys, "argv", ["benchmark.py", *arguments]):
        with contextlib.redirect_stderr(io.StringIO()):
            exec(compile(ast.Module(body=nodes, type_ignores=[]),
                         "benchmark_parser", "exec"), context)
    return context["args"]


class PauseArguments(unittest.TestCase):
    def test_steady_defaults_remain_without_pause(self):
        args = parse([])
        self.assertFalse(args.recovery)
        self.assertEqual(args.recovery_pause_seconds, 0)

    def test_recovery_only_accepts_sufficient_pause_window(self):
        args = parse(["--recovery-only", "--recovery-duration", "90",
                      "--recovery-pause-seconds", "20"])
        self.assertTrue(args.recovery)
        self.assertEqual(args.recovery_pause_seconds, 20)

    def test_invalid_pause_experiments_are_rejected(self):
        for args in (["--recovery-pause-seconds", "20"],
                     ["--recovery", "--recovery-pause-seconds", "-1"],
                     ["--recovery", "--recovery-pause-after", "-1"],
                     ["--recovery", "--recovery-pause-seconds", "20"]):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit):
                    parse(args)


if __name__ == "__main__":
    unittest.main()
