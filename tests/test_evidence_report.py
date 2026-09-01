from __future__ import annotations

import unittest

from zxn_agent.changes import FileChange
from zxn_agent.evidence_report import build_evidence_report, report_markdown
from zxn_agent.state import State


class TestEvidenceReport(unittest.TestCase):
    def test_report_contains_only_runtime_metrics_and_structured_evidence(self):
        st = State()
        st.begin_turn(task="Fix parser and verify all tests")
        st.plan.replace([
            {"step": "Inspect parser behavior", "status": "completed"},
            {"step": "Repair parse boundary", "status": "in_progress"},
        ])
        st.task_model_calls = 3
        st.task_tool_calls = 5
        st.task_in_tok = 120
        st.task_out_tok = 30
        st.task_cache_hit_tok = 90
        st.task_cache_miss_tok = 30
        st.task_reasoning_tok = 20
        st.task_cache_usage_reported = True
        st.task_reasoning_usage_reported = True
        st.step = 4
        st.note_evidence({"kind": "tool", "tool": "read_file", "path": "parser.py"})
        st.note_check_attempt(
            "python -m pytest tests/test_parser.py -q",
            "FAILED tests/test_parser.py::test_value - AssertionError",
            1,
            "targeted",
        )
        st.termination_reason = "max_steps"

        report = build_evidence_report(
            st,
            changes=[FileChange("parser.py", "modified", 4, 2)],
            final_text="Stopped after the configured step limit.",
            elapsed_seconds=2.5,
        )

        self.assertEqual(report["task"], "Fix parser and verify all tests")
        self.assertEqual(report["metrics"]["model_calls"], 3)
        self.assertEqual(report["metrics"]["tool_calls"], 5)
        self.assertEqual(report["metrics"]["checks"], 1)
        self.assertEqual(report["metrics"]["total_tokens"], 150)
        self.assertEqual(report["metrics"]["prompt_cache_hit_tokens"], 90)
        self.assertEqual(report["metrics"]["prompt_cache_miss_tokens"], 30)
        self.assertEqual(report["metrics"]["reasoning_tokens"], 20)
        self.assertEqual(report["changed_files"][0]["path"], "parser.py")
        self.assertEqual(report["outcome"]["termination_reason"], "max_steps")

        rendered = report_markdown(report)
        self.assertIn("# Coding Agent Evidence Report", rendered)
        self.assertIn("Repair progress: failed", rendered)
        self.assertIn("parser.py", rendered)
        self.assertIn("Workspace revision", rendered)
        self.assertIn("Prompt cache hit: 90", rendered)
        self.assertIn("Reasoning tokens: 20", rendered)

    def test_missing_provider_breakdown_is_reported_as_unavailable(self):
        st = State(task_in_tok=10, task_out_tok=2)
        report = build_evidence_report(
            st,
            changes=[],
            final_text="done",
            elapsed_seconds=0.1,
        )

        self.assertIsNone(report["metrics"]["prompt_cache_hit_tokens"])
        self.assertIsNone(report["metrics"]["reasoning_tokens"])
        rendered = report_markdown(report)
        self.assertIn("Prompt cache hit: unavailable", rendered)


if __name__ == "__main__":
    unittest.main()
