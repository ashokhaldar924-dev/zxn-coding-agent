from __future__ import annotations

import os
import unittest

from zxn_agent.gui import _approval_parts, _outcome_html
from zxn_agent.gui_presenter import GuiPresenter, OutcomeView, VerificationView


def verification(current: bool, *, rev: int = 3, verified: int = 2) -> dict:
    return {
        "workspace_revision": rev,
        "verified_revision": verified,
        "required": True,
        "current": current,
        "adequate": current,
        "satisfied": current,
        "fingerprint_matched": current,
        "verifier": "python -m pytest -q",
        "last_check_rc": 0,
        "tracking_complete": True,
        "required_scope": "any",
        "verified_scope": "full" if current else None,
        "task_completed": current,
        "progress": "passed" if current else "not_checked",
        "check_attempts": 1 if current else 0,
    }


class TestGuiPresenter(unittest.TestCase):
    def setUp(self):
        self.presenter = GuiPresenter()

    def test_events_become_compact_activity_without_raw_arguments(self):
        self.presenter.consume(
            {
                "event": "task",
                "text": "Fix parser",
                "workspace": "D:/project",
                "verification": verification(False),
            }
        )
        self.presenter.consume(
            {
                "event": "tool_call",
                "id": "read-1",
                "name": "read_file",
                "arguments": '{"path":"src/parser.py","start":10,"end":40}',
            }
        )
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "read-1",
                "name": "read_file",
                "text": "src/parser.py 10-40 / 100",
                "ok": True,
                "file_changes": [],
            }
        )

        titles = [item.title for item in self.presenter.activity]
        self.assertEqual(titles, ["Fix parser", "已读取 src/parser.py:10-40"])
        self.assertNotIn('"path"', "\n".join(titles))

    def test_plan_is_replaced_from_runtime_plan_event(self):
        self.presenter.consume(
            {
                "event": "plan_update",
                "plan": {
                    "items": [
                        {"step": "Inspect", "status": "completed"},
                        {"step": "Fix", "status": "in_progress"},
                        {"step": "Verify", "status": "pending"},
                    ]
                },
            }
        )
        self.assertEqual(
            [(item.step, item.status) for item in self.presenter.plan],
            [
                ("Inspect", "completed"),
                ("Fix", "in_progress"),
                ("Verify", "pending"),
            ],
        )

    def test_plan_status_updates_replace_the_gui_view_immediately(self):
        steps = [
            "Trace scheduler state transitions",
            "Persist restart recovery",
            "Verify retry boundaries",
        ]
        snapshots = [
            ["in_progress", "pending", "pending"],
            ["completed", "in_progress", "pending"],
            ["completed", "completed", "in_progress"],
        ]
        observed = []
        for statuses in snapshots:
            self.presenter.consume(
                {
                    "event": "plan_update",
                    "plan": {
                        "items": [
                            {"step": step, "status": status}
                            for step, status in zip(steps, statuses)
                        ]
                    },
                }
            )
            observed.append([item.status for item in self.presenter.plan])

        self.assertEqual(observed, snapshots)

    def test_plan_evidence_hint_comes_from_runtime_tool_result(self):
        self.presenter.consume(
            {
                "event": "plan_update",
                "plan": {"items": [{"step": "Inspect parser behavior", "status": "in_progress"}]},
            }
        )
        self.presenter.consume(
            {
                "event": "tool_call",
                "id": "read",
                "name": "read_file",
                "arguments": '{"path":"parser.py"}',
            }
        )
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "read",
                "name": "read_file",
                "text": "parser.py 1-20",
                "ok": True,
                "file_changes": [],
            }
        )
        self.presenter.consume(
            {"event": "model_response", "message": {"content": "I inspected everything."}}
        )

        self.assertEqual(self.presenter.plan[0].evidence, ("read_file parser.py",))

        self.presenter.consume(
            {
                "event": "plan_update",
                "plan": {"items": [{"step": "Inspect parser behavior", "status": "completed"}]},
            }
        )
        self.assertEqual(self.presenter.plan[0].evidence, ("read_file parser.py",))

    def test_file_change_kinds_and_true_counts_are_preserved(self):
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "edit-1",
                "name": "multi_edit",
                "text": "updated",
                "ok": True,
                "file_changes": [
                    {"path": "a.py", "kind": "added", "additions": 8, "deletions": 0},
                    {"path": "b.py", "kind": "modified", "additions": 3, "deletions": 2},
                    {"path": "c.py", "kind": "deleted", "additions": 0, "deletions": 5},
                ],
            }
        )
        changes = [item.change for item in self.presenter.activity if item.change]
        self.assertEqual(
            [(item.kind, item.additions, item.deletions) for item in changes],
            [("added", 8, 0), ("modified", 3, 2), ("deleted", 0, 5)],
        )

    def test_command_pass_and_fail_are_bounded_and_include_duration(self):
        self.presenter.consume(
            {
                "event": "tool_call",
                "id": "check-1",
                "name": "check_command",
                "arguments": '{"cmd":"python -m pytest -q"}',
            }
        )
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "check-1",
                "name": "check_command",
                "text": "exit code: 0\nstdout:\n72 passed in 2.91s",
                "ok": True,
                "rc": 0,
                "elapsed_seconds": 3.02,
                "file_changes": [],
            }
        )
        passed = self.presenter.activity[-1]
        self.assertEqual(passed.tone, "success")
        self.assertIn("72 passed", passed.detail[0])
        self.assertIn("3.0s", passed.detail[0])

        self.presenter.consume(
            {
                "event": "tool_call",
                "id": "check-2",
                "name": "check_command",
                "arguments": '{"cmd":"python -m pytest -q"}',
            }
        )
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "check-2",
                "name": "check_command",
                "text": "exit code: 1\nstderr:\n2 failed, 70 passed\nAssertionError: wrong value",
                "ok": True,
                "rc": 1,
                "elapsed_seconds": 1.5,
                "file_changes": [],
            }
        )
        failed = self.presenter.activity[-1]
        self.assertEqual(failed.tone, "failure")
        self.assertLessEqual(len(failed.detail), 7)
        self.assertIn("2 failed", failed.detail[0])

    def test_verification_current_comes_only_from_runtime_snapshot(self):
        self.presenter.consume({"event": "tool_result", "verification": verification(True, rev=4, verified=4)})
        self.assertTrue(self.presenter.verification.current)
        self.assertTrue(self.presenter.verification.fingerprint_matched)
        self.assertEqual(self.presenter.verification.workspace_revision, 4)

        # Equal revisions are intentionally insufficient when Runtime says stale.
        self.presenter.consume({"event": "model_response", "verification": verification(False, rev=5, verified=5)})
        self.assertFalse(self.presenter.verification.current)
        self.assertFalse(self.presenter.verification.fingerprint_matched)

        # Model prose cannot manufacture verification state.
        self.presenter.consume(
            {
                "event": "model_response",
                "message": {"content": "FINAL VERIFIED", "tool_calls": []},
            }
        )
        self.assertFalse(self.presenter.verification.current)

    def test_stopped_targeted_check_is_current_but_not_adequate(self):
        snapshot = verification(True, rev=8, verified=8)
        snapshot.update(
            adequate=False,
            satisfied=False,
            required_scope="full",
            verified_scope="targeted",
            task_completed=False,
        )
        self.presenter.consume(
            {
                "event": "task",
                "text": "Ensure all tests pass",
                "workspace": "D:/project",
                "plan": {
                    "items": [
                        {"step": "Implement", "status": "completed"},
                        {"step": "Run full verification", "status": "in_progress"},
                    ]
                },
                "verification": snapshot,
            }
        )
        self.presenter.consume(
            {
                "event": "max_steps",
                "message": "Stopped after 30 steps.",
                "verification": snapshot,
            }
        )
        self.presenter.consume(
            {
                "event": "turn_summary",
                "completed": False,
                "text": "Stopped after 30 steps.",
                "changes": [],
                "verification": snapshot,
            }
        )

        view = self.presenter.verification
        self.assertTrue(view.current)
        self.assertFalse(view.adequate)
        self.assertFalse(view.task_completed)
        self.assertIn("计划未完成", self.presenter.activity[-1].detail[-1])
        self.assertEqual(
            sum(item.kind == "completion" for item in self.presenter.activity),
            1,
        )

    def test_task_event_exposes_pre_existing_baseline_changes(self):
        self.presenter.consume(
            {
                "event": "task",
                "text": "Fix code",
                "workspace": "D:/project",
                "initial_dirty": ["existing.py"],
            }
        )
        self.assertEqual(self.presenter.activity[-1].tone, "warning")
        self.assertIn("existing.py", self.presenter.activity[-1].title)

    def test_turn_summary_keeps_final_changes_and_compact_completion(self):
        self.presenter.consume(
            {
                "event": "turn_summary",
                "completed": True,
                "text": "Fixed the implementation and verified the tests.",
                "changes": [
                    {"path": "service.py", "kind": "modified", "additions": 4, "deletions": 2}
                ],
                "verification": verification(True, rev=6, verified=6),
                "steps": 9,
                "elapsed_seconds": 2.5,
                "model_calls": 6,
                "tool_calls": 8,
                "checks": 2,
                "input_tokens": 100,
                "output_tokens": 20,
                "repair_progress": "passed",
                "termination_reason": "completed",
                "report": {
                    "metrics": {
                        "total_tokens": 120,
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_cache_hit_tokens": 70,
                        "prompt_cache_miss_tokens": 30,
                        "reasoning_tokens": 12,
                    }
                },
            }
        )
        self.assertEqual(self.presenter.activity[-1].title, "任务完成")
        self.assertEqual(self.presenter.final_changes[0].path, "service.py")
        self.assertTrue(self.presenter.verification.current)
        self.assertEqual(self.presenter.outcome.status, "final_verified")
        self.assertEqual(self.presenter.outcome.steps, 9)
        self.assertEqual(self.presenter.outcome.model_calls, 6)
        self.assertEqual(self.presenter.outcome.tool_calls, 8)
        self.assertEqual(self.presenter.outcome.checks, 2)
        self.assertEqual(self.presenter.outcome.tokens, 120)
        self.assertEqual(self.presenter.outcome.prompt_tokens, 100)
        self.assertEqual(self.presenter.outcome.completion_tokens, 20)
        self.assertEqual(self.presenter.outcome.cache_hit_tokens, 70)
        self.assertEqual(self.presenter.outcome.cache_miss_tokens, 30)
        self.assertEqual(self.presenter.outcome.reasoning_tokens, 12)
        self.assertIsNotNone(self.presenter.evidence_report)

    def test_stopped_outcome_never_becomes_final_verified(self):
        snapshot = verification(True, rev=8, verified=8)
        snapshot["task_completed"] = False
        self.presenter.consume(
            {
                "event": "turn_summary",
                "completed": False,
                "text": "Stopped by user.",
                "changes": [],
                "steps": 4,
                "verification": snapshot,
            }
        )

        self.assertEqual(self.presenter.outcome.status, "stopped")
        self.assertFalse(self.presenter.outcome.completed)

    def test_command_output_reference_is_kept_for_on_demand_view(self):
        self.presenter.consume(
            {
                "event": "tool_call",
                "id": "cmd",
                "name": "run_command",
                "arguments": '{"cmd":"python -m pytest -q"}',
            }
        )
        self.presenter.consume(
            {
                "event": "tool_result",
                "id": "cmd",
                "name": "run_command",
                "text": "preview",
                "ok": True,
                "rc": 0,
                "output_ref": "cmd-123456abcdef.txt",
                "file_changes": [],
            }
        )

        self.assertEqual(self.presenter.activity[-1].output_ref, "cmd-123456abcdef.txt")

    def test_task_restore_removes_restored_changes_and_marks_verification_stale(self):
        self.presenter.consume(
            {
                "event": "turn_summary",
                "completed": True,
                "text": "done",
                "changes": [
                    {"path": "service.py", "kind": "modified", "additions": 4, "deletions": 2}
                ],
                "verification": verification(True, rev=4, verified=4),
            }
        )
        stale = verification(False, rev=5, verified=-1)
        stale["task_completed"] = False
        self.presenter.consume(
            {
                "event": "task_restore",
                "message": "Restored 1 Agent file checkpoint.",
                "restored_paths": ["service.py"],
                "verification": stale,
                "report": {"outcome": {"termination_reason": "restored_task_changes"}},
            }
        )

        self.assertEqual(self.presenter.changes, [])
        self.assertEqual(self.presenter.outcome.status, "restored")
        self.assertFalse(self.presenter.verification.current)
        self.assertEqual(
            self.presenter.evidence_report["outcome"]["termination_reason"],
            "restored_task_changes",
        )

    def test_secret_is_redacted_even_for_non_log_sources(self):
        previous = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_API_KEY"] = "gui-super-secret"
        try:
            self.presenter.consume(
                {
                    "event": "gui_error",
                    "message": "request contained gui-super-secret",
                }
            )
        finally:
            if previous is None:
                os.environ.pop("AGENT_API_KEY", None)
            else:
                os.environ["AGENT_API_KEY"] = previous
        self.assertNotIn("gui-super-secret", self.presenter.activity[-1].title)

    def test_approval_adapter_preserves_all_three_existing_choices(self):
        body, options = _approval_parts(
            "Command requires approval: unrecognized command.\n\n"
            "python -m pytest tests -q\n\n"
            "  [1] Allow once\n"
            "  [2] Allow this command family for the session: python -m pytest\n"
            "  [3] Deny\n"
            "Choose [1/2/3]: "
        )
        self.assertIn("python -m pytest tests -q", body)
        self.assertEqual(options["1"], "仅允许一次")
        self.assertIn("命令系列", options["2"])
        self.assertEqual(options["3"], "拒绝")

    def test_outcome_card_uses_chinese_product_labels(self):
        outcome = OutcomeView(
            visible=True,
            completed=True,
            status="final_verified",
            changed_files=2,
            model_calls=3,
            tool_calls=4,
            checks=1,
            tokens=1200,
            repair_progress="passed",
        )
        state = VerificationView(
            workspace_revision=2,
            verified_revision=2,
            fingerprint_matched=True,
            verified_scope="full",
        )

        rendered = _outcome_html(outcome, state)

        for text in ("任务完成", "最终验证通过", "改动文件", "模型调用", "全量"):
            self.assertIn(text, rendered)
        self.assertNotIn("Task Completed", rendered)
        self.assertNotIn("FINAL VERIFIED", rendered)


if __name__ == "__main__":
    unittest.main()
