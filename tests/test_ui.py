from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui
from changes import FileChange, summarize_text_change
from planner import PlanState
from state import State, ToolRes


class TestTerminalRenderer(unittest.TestCase):
    def capture(self, callback) -> str:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            callback()
        return stream.getvalue()

    def test_plan_tool_and_file_output_are_compact_without_raw_json(self):
        plan = PlanState()
        plan.replace([
            {"step": "Inspect implementation", "status": "completed"},
            {"step": "Apply focused fix", "status": "in_progress"},
            {"step": "Run verification", "status": "pending"},
        ])

        def render():
            ui.tool_finished(
                "update_plan", {}, ToolRes("hidden", plan_updated=True), plan_state=plan
            )
            ui.tool_finished(
                "search_text",
                {"query": "verification_current", "path": "."},
                ToolRes("a.py:1: value\n\nFound 4 matches; showing first 1."),
            )
            ui.tool_finished(
                "edit_file",
                {"path": "state.py", "old": "raw old text", "new": "raw new text"},
                ToolRes(
                    "Updated state.py",
                    file_changes=[FileChange("state.py", "modified", 3, 1)],
                ),
            )

        output = self.capture(render)
        self.assertIn("[x] Inspect implementation", output)
        self.assertIn("> Apply focused fix", output)
        self.assertIn("[ ] Run verification", output)
        self.assertIn('Searched "verification_current" - 4 matches', output)
        self.assertIn("Modified state.py", output)
        self.assertIn("+3", output)
        self.assertNotIn("raw old text", output)
        self.assertNotIn('"path"', output)

    def test_real_before_after_content_drives_added_modified_deleted_counts(self):
        changes = [
            summarize_text_change("new.py", None, "a\nb\n"),
            summarize_text_change("app.py", "a\nb\n", "a\nc\nd\n"),
            summarize_text_change("old.py", "a\nb\n", None),
        ]
        output = self.capture(lambda: [ui.render_file_change(change) for change in changes])

        self.assertEqual((changes[0].additions, changes[0].deletions), (2, 0))
        self.assertEqual((changes[1].additions, changes[1].deletions), (2, 1))
        self.assertEqual((changes[2].additions, changes[2].deletions), (0, 2))
        self.assertIn("Added new.py", output)
        self.assertIn("Modified app.py", output)
        self.assertIn("Deleted old.py", output)

    def test_command_success_failure_and_secret_redaction(self):
        old_secret = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_API_KEY"] = "terminal-test-secret"
        try:
            def render():
                ui.tool_started("check_command", {"cmd": "pytest -q"})
                ui.tool_finished(
                    "check_command", {}, ToolRes("exit code: 0\nstdout:\n18 passed", rc=0)
                )
                ui.tool_started("run_command", {"cmd": "pytest bad -q"})
                ui.tool_finished(
                    "run_command",
                    {},
                    ToolRes(
                        "exit code: 1\nstderr:\nterminal-test-secret\n1 failed, 17 passed",
                        rc=1,
                    ),
                )

            output = self.capture(render)
        finally:
            if old_secret is None:
                os.environ.pop("AGENT_API_KEY", None)
            else:
                os.environ["AGENT_API_KEY"] = old_secret

        self.assertIn("Verifying with pytest -q", output)
        self.assertIn("OK 18 passed", output)
        self.assertIn("Command failed (exit 1)", output)
        self.assertIn("1 failed, 17 passed", output)
        self.assertNotIn("terminal-test-secret", output)

    def test_final_verification_comes_from_runtime_and_can_become_stale(self):
        root = tempfile.mkdtemp()
        try:
            path = Path(root, "a.py")
            path.write_text("value = 1\n", encoding="utf-8")
            st = State(rev=1, changed=True, completed=True, last_check_cmd="pytest -q")
            st.initialize_workspace_tracking(root)
            st.mark_verified()
            change = FileChange("a.py", "modified", 1, 1)

            verified = self.capture(lambda: ui.finish("Done.", st, [change]))
            path.write_text("value = 2\n", encoding="utf-8")
            st.reconcile_workspace(root)
            stale = self.capture(lambda: ui.finish("Stopped.", st, [change]))
        finally:
            shutil.rmtree(root, ignore_errors=True)

        self.assertIn("FINAL VERIFIED", verified)
        self.assertIn("workspace rev 1 / verified 1", verified)
        self.assertIn("Verification stale", stale)
        self.assertNotIn("FINAL VERIFIED", stale)

    def test_stopped_task_with_targeted_current_check_is_only_partial(self):
        root = tempfile.mkdtemp()
        try:
            Path(root, "a.py").write_text("value = 1\n", encoding="utf-8")
            st = State(
                rev=1,
                changed=True,
                completed=False,
                requires_full_verification=True,
                last_check_cmd="pytest tests/test_a.py -q",
            )
            st.initialize_workspace_tracking(root)
            st.mark_verified("targeted")
            output = self.capture(
                lambda: ui.finish(
                    "Stopped after 30 steps.",
                    st,
                    [FileChange("a.py", "modified", 1, 1)],
                )
            )
        finally:
            shutil.rmtree(root, ignore_errors=True)

        self.assertIn("Task stopped", output)
        self.assertIn("Last Verification", output)
        self.assertIn("PARTIALLY VERIFIED", output)
        self.assertIn("Full-suite verification was not completed", output)
        self.assertNotIn("FINAL VERIFIED", output)

    def test_non_tty_fallback_has_no_ansi_and_hides_attached_file_body(self):
        output = self.capture(
            lambda: ui.user_task(
                "Inspect @a.py\n\nUser-explicit file references:\n<referenced_file>secret body</referenced_file>",
                "D:/project",
            )
        )
        self.assertNotIn("\x1b", output)
        self.assertIn("> Inspect @a.py", output)
        self.assertNotIn("secret body", output)


if __name__ == "__main__":
    unittest.main()
