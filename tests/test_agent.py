from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import config  # noqa: E402
from ctx import Ctx  # noqa: E402
from log import NullLog  # noqa: E402
from state import State  # noqa: E402


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
        return response, {}


class TestAgentLoop(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old = {
            "workspace": config.WORKSPACE_DIR,
            "confirm": config.REQUIRE_CONFIRMATION,
            "steps": config.MAX_STEPS,
            "time": config.MAX_TIME,
            "errors": config.MAX_ERRORS,
            "identical": config.MAX_IDENTICAL_CALLS,
        }
        config.WORKSPACE_DIR = self.tmpdir
        config.REQUIRE_CONFIRMATION = False
        config.MAX_STEPS = 30
        config.MAX_TIME = 600
        config.MAX_ERRORS = 4
        config.MAX_IDENTICAL_CALLS = 3
        self.logger = NullLog()

    def tearDown(self):
        config.WORKSPACE_DIR = self.old["workspace"]
        config.REQUIRE_CONFIRMATION = self.old["confirm"]
        config.MAX_STEPS = self.old["steps"]
        config.MAX_TIME = self.old["time"]
        config.MAX_ERRORS = self.old["errors"]
        config.MAX_IDENTICAL_CALLS = self.old["identical"]
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
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(fake.messages[1][-1]["role"], "tool")

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
        fake = FakeLLM([
            tools_message(
                call("1", "edit_file", {"path": "a.txt", "old": "old", "new": "new"}),
                call("2", "run_command", {"cmd": "echo no"}),
            ),
            {"content": "Stopped safely."},
        ])
        with mock.patch("builtins.input", return_value="n"):
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

    def test_max_steps_and_wall_time_are_runtime_termination_conditions(self):
        config.MAX_STEPS = 1
        fake = FakeLLM([tools_message(call("1", "read_file", {"path": "missing"}))])
        final, _, _ = self.run_agent(fake)
        self.assertIn("Stopped after 1 steps", final)

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
    def test_missing_configuration_is_reported_without_traceback(self):
        with (
            mock.patch("agent._parser") as parser,
            mock.patch("config.get_api_key", side_effect=RuntimeError("missing key")),
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
