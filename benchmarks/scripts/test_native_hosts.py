#!/usr/bin/env python3
"""Native revision comparison and fail-closed output gates, without host data."""

import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import benchmark_native_hosts as native


class NativeComparisonTest(unittest.TestCase):
    def run_comparison(self, defect=None):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            output = root / "out"
            stack.enter_context(patch.object(sys, "argv", [
                "native", "--commit", "candidate", "--baseline-commit", "baseline",
                "--modes", "default,adaptive", "--only", "B02", "--threads", "1,16",
                "--repeats", "3", "--out", str(output),
            ]))
            stack.enter_context(patch.object(native.os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(native.runner, "WORKROOT", root / "builds"))
            stack.enter_context(patch.object(native.runner, "HostLock", return_value=MagicMock()))
            for name in ["verify_datasets", "check_disk_space"]:
                stack.enter_context(patch.object(native.runner, name))
            stack.enter_context(patch.object(native.runner, "rustc_version", return_value="test-rustc"))
            stack.enter_context(patch.object(native.runner, "prepare_worktree", side_effect=lambda revision: root / revision))
            stack.enter_context(patch.object(native, "digest", return_value="test-sha"))
            stack.enter_context(patch.object(native, "warm"))
            stack.enter_context(patch.object(native.subprocess, "check_output", side_effect=lambda command, **kwargs:
                ("a" if command[-1].startswith("candidate") else "b") * 40 + "\n"))

            def execute(command, stdout, **kwargs):
                binary = command[command.index("-s") - 1]
                self.assertTrue(binary.endswith("/shmap"))
                candidate = "a" * 40 in binary
                Path(command[4]).write_text("2\t1\t0.2\t100\n" if candidate else "4\t2\t0.4\t200\n")
                profile = Path(command[command.index("--profile-log") + 1])
                profile.write_text(json.dumps({"global": {
                    "timers_secs": {"mapping": 1, "indexing": 1},
                    "counters": {"adaptive_hits": 2 if defect == "counters" and candidate else 1},
                }}))
                query = "changed" if defect == "paf" and candidate else "read"
                stdout.write(f"{query}\t100\t0\t100\t+\tref\t1000\t0\t100\t100\t100\t255\n")

            stack.enter_context(patch.object(native.subprocess, "run", side_effect=execute))
            if defect:
                with self.assertRaisesRegex(RuntimeError, "parity failure"):
                    native.main()
            else:
                native.main()
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["status"], "failed" if defect else "complete")
            if not defect:
                self.assertEqual(len(report["rows"]), 24)
                self.assertEqual({row["speedup"] for row in report["summary"]
                                  if not row["mode"].startswith("baseline-")}, {2.0})
                self.assertTrue(all(row["deterministic"] and row["work_parity"] for row in report["rows"]))
                self.assertEqual({row["commit"] for row in report["rows"]}, {"a" * 40, "b" * 40})

    def test_comparison_completes(self):
        self.run_comparison()

    def test_paf_difference_fails(self):
        self.run_comparison("paf")

    def test_counter_difference_fails(self):
        self.run_comparison("counters")


if __name__ == "__main__":
    unittest.main()