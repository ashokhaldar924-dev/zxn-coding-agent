from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from zxn_agent import agent, config
from zxn_agent.ctx import Ctx
from zxn_agent.log import NullLog
from zxn_agent.state import State


def call(call_id: str, name: str, args) -> dict:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def tools_message(*calls: dict) -> dict:
    return {"content": "", "tool_calls": list(calls)}


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def __call__(self, messages, schemas):
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("FakeLLM ran out of responses")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response()
        if isinstance(response, tuple):
            return response
        return response, {}


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old = {
            "workspace": config.WORKSPACE_DIR,
            "confirm": config.REQUIRE_CONFIRMATION,
            "permission_mode": config.PERMISSION_MODE,
            "steps": config.MAX_STEPS,
            "time": config.MAX_TIME,
            "errors": config.MAX_ERRORS,
            "identical": config.MAX_IDENTICAL_CALLS,
            "task_tokens": config.MAX_TASK_TOKENS,
        }
        config.WORKSPACE_DIR = self.tmpdir
        config.REQUIRE_CONFIRMATION = False
        config.PERMISSION_MODE = "balanced"
        config.MAX_STEPS = 30
        config.MAX_TIME = 600
        config.MAX_ERRORS = 4
        config.MAX_IDENTICAL_CALLS = 3
        config.MAX_TASK_TOKENS = 0
        self.logger = NullLog()

    def tearDown(self):
        config.WORKSPACE_DIR = self.old["workspace"]
        config.REQUIRE_CONFIRMATION = self.old["confirm"]
        config.PERMISSION_MODE = self.old["permission_mode"]
        config.MAX_STEPS = self.old["steps"]
        config.MAX_TIME = self.old["time"]
        config.MAX_ERRORS = self.old["errors"]
        config.MAX_IDENTICAL_CALLS = self.old["identical"]
        config.MAX_TASK_TOKENS = self.old["task_tokens"]
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_agent(self, fake, st=None):
        st = st or State()
        ctx = Ctx("system", "task")
        with contextlib.redirect_stdout(io.StringIO()):
            final = agent.run_task(ctx, st=st, model_call=fake, logger=self.logger)
        return final, st, ctx

    def test_read_then_final_without_changes(self):
        Path(self.tmpdir, "a.txt").write_text("hello", encoding="utf-8")
        fake = FakeLLM([
            tools_message(call("1", "read_file", {"path": "a.txt"})),
            {"content": "Read complete."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Read complete.")
        self.assertFalse(st.changed)
        self.assertTrue(st.completed)
        self.assertEqual(st.plan.items, [])
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(fake.messages[1][-1]["role"], "tool")

    def test_user_stop_during_model_wait_returns_without_accepting_response(self):
        cancel = threading.Event()
        release = threading.Event()
        st = State(cancel_event=cancel)
        timer = threading.Timer(0.1, cancel.set)

        def slow_model(_messages, _schemas):
            release.wait(5)
            return {"content": "Too late."}, {}

        timer.start()
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                final = agent.run_task(
                    Ctx("system", "task"),
                    st=st,
                    model_call=slow_model,
                    logger=self.logger,
                )
        finally:
            release.set()
            timer.cancel()

        self.assertEqual(final, "Stopped by user.")
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(st.completed)
        self.assertTrue(any(event["event"] == "user_stopped" for event in self.logger.events))

    def test_session_persistence_callback_receives_complete_logical_groups(self):
        Path(self.tmpdir, "a.txt").write_text("hello", encoding="utf-8")
        fake = FakeLLM([
            tools_message(call("1", "read_file", {"path": "a.txt"})),
            {"content": "Done."},
        ])
        persisted = []
        ctx = Ctx("system", "task")
        with contextlib.redirect_stdout(io.StringIO()):
            final = agent.run_task(
                ctx,
                st=State(),
                model_call=fake,
                logger=self.logger,
                persist_group=lambda group, st: persisted.append(group),
            )
        self.assertEqual(final, "Done.")
        self.assertEqual([message["role"] for message in persisted[0]], ["assistant", "tool"])
        self.assertEqual(persisted[0][1]["tool_call_id"], "1")
        self.assertEqual(persisted[1], [{"role": "assistant", "content": "Done."}])

    def test_reasoning_content_is_preserved_for_deepseek_tool_rounds(self):
        Path(self.tmpdir, "a.txt").write_text("hello", encoding="utf-8")
        first = tools_message(call("1", "read_file", {"path": "a.txt"}))
        first["reasoning_content"] = "private reasoning state"
        fake = FakeLLM([first, {"content": "Done."}])
        final, _, _ = self.run_agent(fake)
        self.assertEqual(final, "Done.")
        assistant = fake.messages[1][-2]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["reasoning_content"], "private reasoning state")

    def test_truncated_tool_response_is_not_executed_and_can_recover(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        truncated = tools_message(
            call("truncated", "edit_file", {"path": "a.txt", "old": "old", "new": "bad"})
        )
        truncated["_finish_reason"] = "length"
        check = {"cmd": f'"{sys.executable}" -c "print(1)"'}
        fake = FakeLLM([
            truncated,
            tools_message(
                call("edit", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})
            ),
            tools_message(call("check", "check_command", check)),
            {"content": "Recovered and verified.", "_finish_reason": "stop"},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertEqual(final, "Recovered and verified.")
        self.assertEqual(path.read_text(encoding="utf-8"), "new")
        self.assertEqual((st.rev, st.ok_rev), (1, 1))
        self.assertEqual(fake.messages[1][-1]["role"], "tool")
        self.assertIn("output limit", fake.messages[1][-1]["content"])
        self.assertTrue(any(
            event.get("event") == "model_protocol_issue"
            and event.get("finish_reason") == "length"
            for event in self.logger.events
        ))

    def test_second_consecutive_length_stops_without_executing_partial_calls(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        responses = []
        for call_id in ("one", "two"):
            message = tools_message(
                call(call_id, "edit_file", {"path": "a.txt", "old": "old", "new": call_id})
            )
            message["_finish_reason"] = "length"
            responses.append(message)
        fake = FakeLLM(responses)

        final, st, _ = self.run_agent(fake)

        self.assertIn("INCOMPLETE_MODEL_OUTPUT", final)
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(st.task_tool_calls, 0)
        self.assertEqual(st.termination_reason, "incomplete_model_output")

    def test_content_filter_stops_once_without_executing_tools(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        message = tools_message(
            call("filtered", "edit_file", {"path": "a.txt", "old": "old", "new": "bad"})
        )
        message["_finish_reason"] = "content_filter"
        fake = FakeLLM([message, {"content": "must not be requested"}])

        final, st, _ = self.run_agent(fake)

        self.assertIn("provider filtered", final)
        self.assertEqual(len(fake.messages), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "old")
        self.assertEqual(st.termination_reason, "content_filter")

    def test_third_identical_check_failure_stops_as_no_progress(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("v0", encoding="utf-8")
        cmd = (
            f'"{sys.executable}" -c "import sys; '
            "print('FAILED tests/test_x.py::test_value - AssertionError: expected 2'); "
            'sys.exit(1)"'
        )
        fake = FakeLLM([
            tools_message(call("check-1", "check_command", {"cmd": cmd})),
            tools_message(call("edit-1", "edit_file", {"path": "a.txt", "old": "v0", "new": "v1"})),
            tools_message(call("check-2", "check_command", {"cmd": cmd})),
            tools_message(call("edit-2", "edit_file", {"path": "a.txt", "old": "v1", "new": "v2"})),
            tools_message(call("check-3", "check_command", {"cmd": cmd})),
            {"content": "must not be requested"},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertIn("NO_PROGRESS", final)
        self.assertEqual(st.repair_progress, "no_progress")
        self.assertEqual(st.repeated_failure_streak, 3)
        self.assertEqual(len(st.check_attempts), 3)
        self.assertEqual(len(fake.messages), 5)
        self.assertFalse(st.completed)

    def test_edit_check_success_then_final(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})),
            tools_message(call("2", "check_command", {"cmd": f'"{sys.executable}" -c "print(1)"'})),
            {"content": "Done."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Done.")
        self.assertEqual((st.rev, st.ok_rev), (1, 1))

    def test_incomplete_plan_is_visible_but_does_not_replace_final_verification(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        check = {"cmd": f'"{sys.executable}" -c "print(1)"'}
        fake = FakeLLM([
            tools_message(call("p", "update_plan", {
                "plan": [
                    {"step": "Change the file", "status": "in_progress"},
                    {"step": "Review the result", "status": "pending"},
                ]
            })),
            tools_message(call("e", "edit_file", {
                "path": "a.txt", "old": "old", "new": "new"
            })),
            tools_message(call("c", "check_command", check)),
            {"content": "Done with objective verification."},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertEqual(final, "Done with objective verification.")
        self.assertEqual((st.rev, st.ok_rev), (1, 1))
        self.assertEqual(st.plan.completed, 0)
        events = [event for event in self.logger.events if event["event"] == "plan_update"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["plan"], st.plan.to_data())

    def test_unverified_final_is_rejected_then_can_recover(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})),
            {"content": "Premature done."},
            tools_message(call("2", "check_command", {"cmd": f'"{sys.executable}" -c "print(1)"'})),
            {"content": "Actually done."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Actually done.")
        self.assertIn("has not been successfully verified", fake.messages[2][-1]["content"])
        self.assertEqual((st.rev, st.ok_rev), (1, 1))

    def test_edit_after_check_invalidates_old_verification(self):
        Path(self.tmpdir, "a.txt").write_text("one", encoding="utf-8")
        check = {"cmd": f'"{sys.executable}" -c "print(1)"'}
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "one", "new": "two"})),
            tools_message(call("2", "check_command", check)),
            tools_message(call("3", "edit_file", {"path": "a.txt", "old": "two", "new": "three"})),
            {"content": "Done too soon."},
            tools_message(call("4", "check_command", check)),
            {"content": "Done after recheck."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Done after recheck.")
        self.assertIn("has not been successfully verified", fake.messages[4][-1]["content"])
        self.assertEqual((st.rev, st.ok_rev), (2, 2))

    def test_failed_verification_then_fix_reverify_and_finish(self):
        path = Path(self.tmpdir, "answer.txt")
        path.write_text("old", encoding="utf-8")
        verifier = (
            f'"{sys.executable}" -c "from pathlib import Path; import sys; '
            "sys.exit(Path('answer.txt').read_text(encoding='utf-8') != 'right')\""
        )
        fake = FakeLLM([
            tools_message(
                call("1", "edit_file", {"path": "answer.txt", "old": "old", "new": "wrong"})
            ),
            tools_message(call("2", "check_command", {"cmd": verifier})),
            tools_message(
                call(
                    "3",
                    "edit_file",
                    {"path": "answer.txt", "old": "wrong", "new": "right"},
                )
            ),
            tools_message(call("4", "check_command", {"cmd": verifier})),
            {"content": "Fixed and verified."},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertEqual(final, "Fixed and verified.")
        self.assertEqual(path.read_text(encoding="utf-8"), "right")
        self.assertEqual((st.rev, st.ok_rev), (2, 2))
        checks = [
            event
            for event in self.logger.events
            if event.get("event") == "tool_result" and event.get("name") == "check_command"
        ]
        self.assertEqual([event["rc"] for event in checks], [1, 0])
        self.assertIn("elapsed_seconds", self.logger.events[-1])

    def test_configured_final_verifier_rejects_a_different_successful_check(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        required = f'"{sys.executable}" -c "print(\'required\')"'
        other = f'"{sys.executable}" -c "print(\'other\')"'
        st = State(required_verifier=required)
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})),
            tools_message(call("2", "check_command", {"cmd": other})),
            {"content": "Done too early."},
            tools_message(call("3", "check_command", {"cmd": required})),
            {"content": "Done with required verifier."},
        ])
        final, st, _ = self.run_agent(fake, st)
        self.assertEqual(final, "Done with required verifier.")
        self.assertIn("did not satisfy", fake.messages[2][-1]["content"])
        self.assertIn("has not been successfully verified", fake.messages[3][-1]["content"])
        self.assertEqual(st.ok_rev, st.rev)

    def test_explicit_full_suite_requirement_rejects_targeted_check(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        tests = Path(self.tmpdir, "tests")
        tests.mkdir()
        Path(tests, "test_sample.py").write_text(
            "def test_ok():\n    assert True\n",
            encoding="utf-8",
        )
        targeted = f'"{sys.executable}" -m pytest tests/test_sample.py -q'
        full = f'"{sys.executable}" -m pytest tests -q'
        st = State(requires_full_verification=True)
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})),
            tools_message(call("2", "check_command", {"cmd": targeted})),
            {"content": "Done after targeted tests."},
            tools_message(call("3", "check_command", {"cmd": full})),
            {"content": "Done after the full suite."},
        ])

        final, st, _ = self.run_agent(fake, st)

        self.assertEqual(final, "Done after the full suite.")
        self.assertTrue(st.completed)
        self.assertTrue(st.verification_adequate())
        self.assertEqual(st.verified_scope, "full")
        self.assertIn("full test suite", fake.messages[3][-1]["content"])
        checks = [
            event for event in self.logger.events
            if event.get("event") == "tool_result" and event.get("name") == "check_command"
        ]
        self.assertTrue(checks[0]["verification"]["current"])
        self.assertFalse(checks[0]["verification"]["adequate"])
        self.assertTrue(checks[1]["verification"]["adequate"])

    def test_run_command_invalidates_verification_and_final_gate_requires_recheck(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        check = f'"{sys.executable}" -c "print(\'ok\')"'
        shell_write = (
            f'"{sys.executable}" -c "from pathlib import Path; '
            "Path('a.txt').write_text('shell', encoding='utf-8')\""
        )
        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "agent"})),
            tools_message(call("2", "check_command", {"cmd": check})),
            tools_message(call("3", "run_command", {"cmd": shell_write})),
            {"content": "Done too early."},
            tools_message(call("4", "check_command", {"cmd": check})),
            {"content": "Done after recheck."},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertEqual(final, "Done after recheck.")
        self.assertEqual(path.read_text(encoding="utf-8"), "shell")
        self.assertTrue(st.external_change_possible)
        self.assertEqual(st.rev, 2)
        self.assertEqual(st.files, {"a.txt"})
        self.assertEqual(st.ok_rev, st.rev)
        self.assertIn("has not been successfully verified", fake.messages[4][-1]["content"])
        shell_result = next(
            event
            for event in self.logger.events
            if event.get("event") == "tool_result" and event.get("name") == "run_command"
        )
        self.assertEqual(shell_result["changed_files"], ["a.txt"])
        self.assertTrue(shell_result["workspace_scan_complete"])

    def test_external_change_after_verifier_is_detected_before_final(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        check = {"cmd": f'"{sys.executable}" -c "print(1)"'}

        def external_change_then_final():
            path.write_text("changed outside", encoding="utf-8")
            return {"content": "Done against stale verification."}

        fake = FakeLLM([
            tools_message(call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"})),
            tools_message(call("2", "check_command", check)),
            external_change_then_final,
            tools_message(call("3", "check_command", check)),
            {"content": "Done after verifying the current workspace."},
        ])

        final, st, _ = self.run_agent(fake)

        self.assertEqual(final, "Done after verifying the current workspace.")
        self.assertEqual(path.read_text(encoding="utf-8"), "changed outside")
        self.assertEqual((st.rev, st.ok_rev), (2, 2))
        self.assertIn("changed after the last successful verification", fake.messages[3][-1]["content"])
        self.assertTrue(any(
            event.get("event") == "workspace_reconcile"
            and event.get("changed_files") == ["a.txt"]
            for event in self.logger.events
        ))

    def test_bad_tool_argument_json_is_an_observation(self):
        fake = FakeLLM([
            tools_message(call("1", "read_file", "{bad json")),
            {"content": "Recovered."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Recovered.")
        self.assertIn("Could not parse tool arguments", fake.messages[1][-1]["content"])
        self.assertEqual(st.errs, 1)

    def test_user_rejections_are_observations_not_errors(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        config.REQUIRE_CONFIRMATION = True
        config.PERMISSION_MODE = "manual"
        fake = FakeLLM([
            tools_message(
                call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"}),
                call("2", "run_command", {"cmd": "custom-tool action"}),
            ),
            {"content": "Stopped safely."},
        ])
        with mock.patch("builtins.input", side_effect=["3", "3"]):
            final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Stopped safely.")
        self.assertEqual(st.errs, 0)
        self.assertEqual(st.rev, 0)
        self.assertEqual(Path(self.tmpdir, "a.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual(
            sum(event["event"] == "user_rejection" for event in self.logger.events), 2
        )

    def test_consecutive_runtime_errors_stop_the_loop(self):
        fake = FakeLLM([
            tools_message(*[call(str(i), "missing_tool", {}) for i in range(4)])
        ])
        final, st, _ = self.run_agent(fake)
        self.assertIn("4 consecutive runtime/tool errors", final)
        self.assertEqual(st.errs, 4)

    def test_step_time_and_task_token_budgets_are_runtime_termination_conditions(self):
        config.MAX_STEPS = 1
        fake = FakeLLM([tools_message(call("1", "read_file", {"path": "missing"}))])
        final, stopped_state, _ = self.run_agent(fake)
        self.assertIn("Stopped after 1 steps", final)
        self.assertFalse(stopped_state.completed)

        called = False
        def should_not_call(messages, schemas):
            nonlocal called
            called = True
            return {"content": "no"}, {}

        st = State(start=time.time() - 10)
        config.MAX_TIME = 1
        final, _, _ = self.run_agent(should_not_call, st)
        self.assertIn("Stopped after 1 seconds", final)
        self.assertFalse(called)

        config.MAX_STEPS = 30
        config.MAX_TIME = 600
        config.MAX_TASK_TOKENS = 10
        Path(self.tmpdir, "budget.txt").write_text("evidence", encoding="utf-8")
        budget_fake = FakeLLM([
            (
                tools_message(call("budget", "read_file", {"path": "budget.txt"})),
                {"input_tokens": 7, "output_tokens": 4},
            ),
            {"content": "must not be requested"},
        ])
        final, budget_state, _ = self.run_agent(budget_fake)
        self.assertIn("task token budget (11/10)", final)
        self.assertEqual(budget_state.task_tokens, 11)
        self.assertEqual(len(budget_fake.messages), 1)

        config.MAX_TASK_TOKENS = 20
        warning_fake = FakeLLM([
            (
                tools_message(call("warning", "read_file", {"path": "budget.txt"})),
                {"input_tokens": 12, "output_tokens": 4},
            ),
            {"content": "Finished within budget."},
        ])
        final, _, _ = self.run_agent(warning_fake)
        self.assertEqual(final, "Finished within budget.")
        self.assertIn("approaching limit", warning_fake.messages[1][0]["content"])

    def test_ctrl_c_is_a_controlled_stop(self):
        fake = FakeLLM([KeyboardInterrupt()])
        final, _, _ = self.run_agent(fake)
        self.assertEqual(final, "Stopped by user (Ctrl+C).")

    def test_third_identical_tool_call_is_blocked_as_stagnation(self):
        Path(self.tmpdir, "a.txt").write_text("hello", encoding="utf-8")
        same = {"path": "a.txt"}
        fake = FakeLLM([
            tools_message(call("1", "read_file", same)),
            tools_message(call("2", "read_file", same)),
            tools_message(call("3", "read_file", same)),
            {"content": "Changed approach."},
        ])
        final, st, _ = self.run_agent(fake)
        self.assertEqual(final, "Changed approach.")
        self.assertIn("Stagnation guard blocked", fake.messages[3][-1]["content"])
        self.assertEqual(st.errs, 0)
        self.assertTrue(any(
            event["event"] == "tool_block" and event["block_kind"] == "stagnation"
            for event in self.logger.events
        ))


class TestCLI(unittest.TestCase):
    def test_stale_git_base_requires_explicit_confirmation(self):
        with mock.patch("builtins.input", return_value="y"), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertTrue(agent._confirm_stale_git_base("a" * 40, "b" * 40))
        with mock.patch("builtins.input", return_value=""), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertFalse(agent._confirm_stale_git_base("a" * 40, "b" * 40))

        store = mock.Mock(session_id="session-test")
        store.load.return_value = mock.Mock(expected_git_head="old-head")
        guard = agent.GitGuard(head="new-head")
        with (
            mock.patch("zxn_agent.agent._scan_workspace", return_value=(guard, [], None)),
            mock.patch("zxn_agent.agent.SessionStore.open", return_value=store),
            mock.patch("zxn_agent.agent._confirm_stale_git_base", return_value=False) as confirm,
            self.assertRaises(agent.SessionError),
        ):
            agent._resume_active("latest", NullLog())
        confirm.assert_called_once_with("old-head", "new-head")
        store.record_git_base.assert_not_called()

    def test_missing_configuration_is_reported_without_traceback(self):
        with (
            mock.patch("zxn_agent.agent._parser") as parser,
            mock.patch("zxn_agent.config.get_api_key", side_effect=RuntimeError("missing key")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            parser.return_value.parse_args.return_value = mock.Mock(
                workspace=None, yes=False, task=["task"]
            )
            rc = agent.main()
        self.assertEqual(rc, 2)
        self.assertIn("Configuration error: missing key", output.getvalue())


if __name__ == "__main__":
    unittest.main()
