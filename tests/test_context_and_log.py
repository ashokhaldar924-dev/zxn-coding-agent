from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from ctx import Ctx  # noqa: E402
from log import RunLog  # noqa: E402


class TestContext(unittest.TestCase):
    def setUp(self):
        self.old_groups = config.MAX_GROUPS
        config.MAX_GROUPS = 2

    def tearDown(self):
        config.MAX_GROUPS = self.old_groups

    def test_head_is_always_preserved(self):
        ctx = Ctx("system", "original task")
        ctx.add_group([{"role": "assistant", "content": "done"}])
        messages = ctx.build()
        self.assertEqual(messages[:2], [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "original task"},
        ])

    def test_old_groups_are_trimmed_as_whole_groups(self):
        ctx = Ctx("system", "task")
        for number in range(3):
            ctx.add_group([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": f"call-{number}", "function": {"name": "x", "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": f"call-{number}", "content": f"result-{number}"},
            ])
        messages = ctx.build()
        text = json.dumps(messages)
        self.assertNotIn("call-0", text)
        self.assertIn("call-1", text)
        self.assertIn("call-2", text)
        for message in messages:
            if message.get("role") == "tool":
                call_id = message["tool_call_id"]
                self.assertTrue(any(
                    call_id == call["id"]
                    for candidate in messages
                    for call in candidate.get("tool_calls", [])
                ))

    def test_build_returns_a_copy(self):
        ctx = Ctx("system", "task")
        ctx.add_group([{"role": "assistant", "content": "answer"}])
        built = ctx.build()
        built[-1]["content"] = "mutated"
        self.assertEqual(ctx.build()[-1]["content"], "answer")


class TestRunLog(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_key = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_API_KEY"] = "super-secret-value"

    def tearDown(self):
        if self.old_key is None:
            os.environ.pop("AGENT_API_KEY", None)
        else:
            os.environ["AGENT_API_KEY"] = self.old_key
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_jsonl_events_are_complete_and_secret_is_redacted(self):
        logger = RunLog(self.tmpdir)
        logger.event("task", text="hello")
        logger.event(
            "fatal_error",
            message="do not leak super-secret-value",
            authorization="Bearer super-secret-value",
        )
        lines = logger.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        events = [json.loads(line) for line in lines]
        self.assertEqual([event["event"] for event in events], ["task", "fatal_error"])
        self.assertNotIn("super-secret-value", "\n".join(lines))
        self.assertEqual(events[1]["authorization"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
