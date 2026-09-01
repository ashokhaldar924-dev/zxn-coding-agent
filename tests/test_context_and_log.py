from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from zxn_agent import config
from zxn_agent.ctx import Ctx
from zxn_agent.log import RunLog


class TestContext(unittest.TestCase):
    def setUp(self):
        self.old_groups = config.MAX_GROUPS
        self.old_chars = config.MAX_CONTEXT_CHARS
        self.old_tokens = config.MAX_CONTEXT_TOKENS
        self.old_keep = config.CONTEXT_KEEP_FULL_GROUPS
        config.MAX_GROUPS = 2
        config.MAX_CONTEXT_CHARS = 60_000
        config.MAX_CONTEXT_TOKENS = 32_000
        config.CONTEXT_KEEP_FULL_GROUPS = 1

    def tearDown(self):
        config.MAX_GROUPS = self.old_groups
        config.MAX_CONTEXT_CHARS = self.old_chars
        config.MAX_CONTEXT_TOKENS = self.old_tokens
        config.CONTEXT_KEEP_FULL_GROUPS = self.old_keep

    def test_head_is_always_preserved(self):
        ctx = Ctx("system", "original task")
        ctx.add_group([{"role": "assistant", "content": "done"}])
        messages = ctx.build()
        self.assertEqual(messages[:2], [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "original task"},
        ])

    def test_runtime_state_is_injected_into_the_system_message(self):
        ctx = Ctx("stable system", "task")
        ctx.add_group([{"role": "assistant", "content": "progress"}])

        messages = ctx.build("runtime revision 2")

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("stable system", messages[0]["content"])
        self.assertIn("runtime revision 2", messages[0]["content"])

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

    def test_old_large_command_output_is_pruned_before_recent_group(self):
        config.MAX_CONTEXT_CHARS = 700
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "old", "function": {"name": "run_command", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "old",
                "content": "X" * 2_000 + " cmd-012345abcdef.txt",
            },
        ])
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "new", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "new", "content": "recent result"},
        ])
        messages = ctx.build()
        text = json.dumps(messages)
        self.assertIn("older tool output pruned", text)
        self.assertIn("cmd-012345abcdef.txt", text)
        self.assertIn("recent result", text)
        self.assertEqual(ctx.last_stats.pruned_tool_outputs, 1)
        self.assertFalse(ctx.last_stats.over_budget)

    def test_old_large_file_output_is_replaced_without_source_body(self):
        config.MAX_CONTEXT_CHARS = 650
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "read-old",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"app.py","start":1,"end":120}',
                    },
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "read-old",
                "content": "app.py 1-120 / 120\n" + "X" * 2_000,
            },
        ])
        ctx.add_group([{"role": "assistant", "content": "recent"}])

        text = json.dumps(ctx.build())

        self.assertIn("older tool output pruned", text)
        self.assertNotIn("X" * 100, text)

    def test_retained_source_stays_in_tool_role_and_is_not_duplicated(self):
        payload = "app.py 1-1 / 1\n\n1 | UNIQUE_SOURCE\n\n0 lines above, 0 lines below."
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "actual provider reasoning",
                "tool_calls": [{
                    "id": "original",
                    "function": {"name": "read_file", "arguments": '{}'},
                }],
            },
            {"role": "tool", "tool_call_id": "original", "content": payload},
        ])
        ctx.add_group([{"role": "assistant", "content": "newer-1"}])
        ctx.add_group([{"role": "assistant", "content": "newer-2"}])

        messages = ctx.build("runtime facts", retained_evidence=[payload])

        self.assertNotIn("UNIQUE_SOURCE", messages[0]["content"])
        self.assertEqual(
            sum("UNIQUE_SOURCE" in str(message.get("content")) for message in messages),
            1,
        )
        source_message = next(
            message for message in messages if "UNIQUE_SOURCE" in str(message.get("content"))
        )
        self.assertEqual(source_message["role"], "tool")
        assistant = next(
            message
            for message in messages
            if any(call.get("id") == "original" for call in message.get("tool_calls", []))
        )
        self.assertEqual(assistant["reasoning_content"], "actual provider reasoning")
        self.assertEqual(ctx.last_retained_evidence, frozenset({payload}))

    def test_bounded_working_set_can_retain_multiple_exact_read_groups(self):
        first = "a.py 1-1 / 1\n\n1 | FIRST_SOURCE\n\n0 lines above, 0 lines below."
        second = "b.py 1-1 / 1\n\n1 | SECOND_SOURCE\n\n0 lines above, 0 lines below."
        ctx = Ctx("system", "task")
        for call_id, payload in (("read-a", first), ("read-b", second)):
            ctx.add_group([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": call_id,
                        "function": {"name": "read_file", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": call_id, "content": payload},
            ])
        ctx.add_group([{"role": "assistant", "content": "newer-1"}])
        ctx.add_group([{"role": "assistant", "content": "newer-2"}])

        messages = ctx.build(retained_evidence=[first, second])
        serialized = json.dumps(messages)

        self.assertIn("FIRST_SOURCE", serialized)
        self.assertIn("SECOND_SOURCE", serialized)
        self.assertEqual(ctx.last_retained_evidence, frozenset({first, second}))

    def test_small_read_does_not_pin_a_group_with_huge_reasoning_overhead(self):
        payload = "small.py 1-1 / 1\n\n1 | value = 1\n\n0 lines above, 0 lines below."
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "R" * 20_000,
                "tool_calls": [{
                    "id": "expensive-read",
                    "function": {"name": "read_file", "arguments": '{}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "expensive-read",
                "content": payload,
            },
        ])
        ctx.add_group([{"role": "assistant", "content": "newer-1"}])
        ctx.add_group([{"role": "assistant", "content": "newer-2"}])

        messages = ctx.build(retained_evidence=[payload])

        self.assertNotIn("value = 1", json.dumps(messages))
        self.assertEqual(ctx.last_retained_evidence, frozenset())

    def test_oversized_old_group_is_dropped_whole(self):
        config.MAX_CONTEXT_CHARS = 500
        ctx = Ctx("system", "task")
        ctx.add_group([{"role": "assistant", "content": "old-" + "X" * 2_000}])
        ctx.add_group([{"role": "assistant", "content": "recent"}])
        messages = ctx.build()
        text = json.dumps(messages)
        self.assertNotIn("old-", text)
        self.assertIn("recent", text)
        self.assertEqual(ctx.last_stats.dropped_groups, 1)

    def test_latest_group_is_preserved_even_when_it_exceeds_budget(self):
        config.MAX_CONTEXT_CHARS = 200
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "latest", "function": {"name": "x", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "latest", "content": "LATEST-" + "X" * 1_000},
        ])
        messages = ctx.build()
        self.assertIn("LATEST-", json.dumps(messages))
        self.assertEqual(ctx.last_stats.pruned_tool_outputs, 0)
        self.assertTrue(ctx.last_stats.over_budget)

    def test_token_budget_prunes_even_when_character_budget_is_large(self):
        config.MAX_CONTEXT_CHARS = 1_000_000
        config.MAX_CONTEXT_TOKENS = 180
        ctx = Ctx("system", "task")
        ctx.add_group([
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "old", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "old", "content": "old-output-" + "X" * 2_000},
        ])
        ctx.add_group([{"role": "assistant", "content": "recent"}])
        messages = ctx.build("runtime-anchor")
        self.assertNotIn("old-output-", json.dumps(messages))
        self.assertIn("runtime-anchor", json.dumps(messages))
        self.assertLessEqual(ctx.last_stats.after_tokens, ctx.last_stats.before_tokens)

    def test_reserved_request_tokens_participate_in_context_budget(self):
        config.MAX_CONTEXT_CHARS = 1_000_000
        ctx = Ctx("system", "task")
        ctx.add_group([{"role": "assistant", "content": "old-output-" + "X" * 800}])
        ctx.add_group([{"role": "assistant", "content": "recent"}])
        ctx.build()
        config.MAX_CONTEXT_TOKENS = ctx.last_stats.after_tokens + 10

        messages = ctx.build(reserved_tokens=80)

        self.assertNotIn("old-output-", json.dumps(messages))
        self.assertIn("recent", json.dumps(messages))
        self.assertEqual(ctx.last_stats.reserved_tokens, 80)
        self.assertEqual(
            ctx.last_stats.estimated_window_tokens,
            ctx.last_stats.after_tokens + 80,
        )

    def test_multi_turn_history_is_durable_but_current_task_stays_anchored(self):
        config.MAX_GROUPS = 2
        ctx = Ctx("system", "first task")
        ctx.add_group([{"role": "assistant", "content": "first answer"}])
        ctx.start_task("second task")
        ctx.add_group([{"role": "assistant", "content": "second answer"}])

        self.assertIn("first task", json.dumps(ctx.history_groups))
        messages = ctx.build("state")
        serialized = json.dumps(messages)
        self.assertIn("second task", serialized)
        self.assertIn("second answer", serialized)
        self.assertIn("state", serialized)


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

    def test_optional_event_sink_receives_only_redacted_events(self):
        received = []
        logger = RunLog(self.tmpdir, event_sink=received.append)
        logger.event("tool_result", text="super-secret-value")

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["event"], "tool_result")
        self.assertEqual(received[0]["text"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
