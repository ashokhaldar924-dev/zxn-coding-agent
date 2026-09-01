from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evals.cases import CASES
from evals.run_eval import (
    _hash_tests,
    _run_hidden_verifier,
    _run_verifier,
    _trajectory_metrics,
    materialize,
)


class TestEvalFixtures(unittest.TestCase):
    def test_generated_bytecode_does_not_count_as_a_test_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            tests = workspace / "tests"
            cache = tests / "__pycache__"
            cache.mkdir(parents=True)
            (tests / "test_example.py").write_text("assert True\n", encoding="utf-8")
            before = _hash_tests(workspace)
            (cache / "test_example.cpython-312.pyc").write_bytes(b"generated")
            self.assertEqual(_hash_tests(workspace), before)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_eight_real_repair_fixtures_are_initially_failing_and_bounded(self):
        self.assertEqual(len(CASES), 8)
        for case in CASES:
            workspace = Path(self.tmpdir, case["name"])
            materialize(case, workspace)
            self.assertNotEqual(_run_verifier(workspace).returncode, 0, case["name"])
            self.assertNotEqual(
                _run_hidden_verifier(case, workspace, Path(self.tmpdir), "baseline").returncode,
                0,
                case["name"],
            )
            hashes = _hash_tests(workspace)
            self.assertTrue(hashes)
            self.assertTrue((workspace / ".agent-verifier").is_file())
            self.assertFalse(any(workspace.rglob("test_hidden_*.py")))

    def test_trajectory_metrics_capture_first_check_recovery_cost_and_time(self):
        workspace = Path(self.tmpdir, "metrics")
        log_dir = workspace / ".agent"
        log_dir.mkdir(parents=True)
        events = [
            {
                "event": "model_response",
                "step": 1,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 40,
                    "reasoning_tokens": 20,
                },
            },
            {"event": "tool_call", "step": 1, "name": "check_command"},
            {
                "event": "tool_result",
                "step": 1,
                "name": "check_command",
                "rc": 1,
                "output_ref": "cmd-example.txt",
            },
            {"event": "tool_call", "step": 2, "name": "edit_file"},
            {
                "event": "tool_result",
                "step": 2,
                "name": "edit_file",
                "changed_files": ["a.py"],
            },
            {"event": "tool_call", "step": 3, "name": "check_command"},
            {"event": "tool_result", "step": 3, "name": "check_command", "rc": 0},
            {
                "event": "final",
                "step": 4,
                "input_tokens": 120,
                "output_tokens": 30,
                "elapsed_seconds": 2.5,
                "revision": 1,
                "verified_revision": 1,
                "files": ["a.py"],
            },
        ]
        (log_dir / "run-test.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

        metrics = _trajectory_metrics(workspace)

        self.assertEqual(metrics["verification_attempts"], 2)
        self.assertEqual(metrics["first_check_rc"], 1)
        self.assertFalse(metrics["first_check_passed"])
        self.assertEqual(metrics["first_successful_check_step"], 3)
        self.assertEqual(metrics["workspace_change_events"], 1)
        self.assertEqual(metrics["saved_command_outputs"], 1)
        self.assertEqual(metrics["tokens"], 150)
        self.assertEqual(metrics["prompt_tokens"], 120)
        self.assertEqual(metrics["completion_tokens"], 30)
        self.assertEqual(metrics["prompt_cache_hit_tokens"], 80)
        self.assertEqual(metrics["prompt_cache_miss_tokens"], 40)
        self.assertEqual(metrics["reasoning_tokens"], 20)
        self.assertEqual(metrics["task_elapsed_seconds"], 2.5)
        self.assertEqual(metrics["model_calls"], 1)
        self.assertFalse(metrics["no_progress"])


if __name__ == "__main__":
    unittest.main()
