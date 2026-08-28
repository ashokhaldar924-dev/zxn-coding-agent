from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkpoint import CheckpointManager
from session import (
    SessionError,
    SessionStore,
    reconcile_checkpoint_state,
    restore_state,
)
from state import State


class TestSession(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_round_trip_preserves_messages_but_resets_process_local_safety(self):
        store = SessionStore.create(self.tmpdir, "model-a", "first task")
        st = State(
            rev=2,
            ok_rev=2,
            changed=True,
            files={"a.py"},
            in_tok=10,
            out_tok=4,
            task_in_tok=6,
            task_out_tok=2,
            session_id=store.session_id,
        )
        tool_group = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        store.record_group(tool_group, st)
        store.record_task("follow-up task")
        store.record_group([{"role": "assistant", "content": "done"}], st)

        reopened = SessionStore.open(self.tmpdir, store.session_id)
        loaded = reopened.load("fresh system")
        restored = restore_state(loaded.state, reopened.session_id)

        self.assertEqual(loaded.ctx.current_task, "follow-up task")
        self.assertIn("c1", json.dumps(loaded.ctx.history_groups))
        self.assertEqual(loaded.ctx.groups[-1][0]["content"], "done")
        self.assertEqual((restored.rev, restored.changed, restored.files), (2, True, {"a.py"}))
        self.assertEqual(restored.ok_rev, -1)
        self.assertEqual(restored.task_tokens, 8)
        self.assertEqual(restored.errs, 0)
        self.assertEqual(restored.repetition.count, 0)
        self.assertFalse(restored.permissions.allow_clean_edits)
        self.assertEqual(loaded.previous_verified_revision, 2)

    def test_incomplete_final_jsonl_line_is_ignored(self):
        store = SessionStore.create(self.tmpdir, "model-a", "task")
        with store.path.open("a", encoding="utf-8") as stream:
            stream.write('{"type":"group"')
        loaded = store.load("system")
        self.assertEqual(loaded.ctx.current_task, "task")

    def test_session_selector_cannot_escape_workspace_store(self):
        SessionStore.create(self.tmpdir, "model-a", "task")
        with self.assertRaises(SessionError):
            SessionStore.open(self.tmpdir, "../outside.jsonl")

    def test_resume_reconciles_checkpoint_newer_than_durable_state(self):
        manager = CheckpointManager(self.tmpdir, "session-test")
        target = Path(self.tmpdir, "a.py")
        target.write_bytes(b"old")
        prepared = manager.prepare("a.py", b"old", b"new", revision_before=0)
        target.write_bytes(b"new")
        manager.commit(prepared, revision_after=1)
        st = State(session_id="session-test")

        reconciled = reconcile_checkpoint_state(st, manager.active())

        self.assertEqual(reconciled, ["a.py"])
        self.assertEqual(st.rev, 1)
        self.assertTrue(st.changed)
        self.assertEqual(st.files, {"a.py"})
        self.assertEqual(st.ok_rev, -1)


if __name__ == "__main__":
    unittest.main()
