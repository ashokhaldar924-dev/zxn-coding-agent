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

import config
import tools
from checkpoint import CheckpointError, CheckpointManager
from state import State


class TestCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_workspace = config.WORKSPACE_DIR
        self.old_confirm = config.REQUIRE_CONFIRMATION
        config.WORKSPACE_DIR = self.tmpdir
        config.REQUIRE_CONFIRMATION = False
        self.manager = CheckpointManager(self.tmpdir, "session-test")
        self.st = State(checkpoints=self.manager)

    def tearDown(self):
        config.WORKSPACE_DIR = self.old_workspace
        config.REQUIRE_CONFIRMATION = self.old_confirm
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def edit(self, path: str, old: str, new: str):
        with contextlib.redirect_stdout(io.StringIO()):
            return tools.run_tool(
                "edit_file",
                {"path": path, "old": old, "new": new},
                self.st,
            )

    def write(self, path: str, content: str):
        with contextlib.redirect_stdout(io.StringIO()):
            return tools.run_tool(
                "write_file",
                {"path": path, "content": content},
                self.st,
            )

    def test_existing_file_is_restored_from_exact_before_image(self):
        path = Path(self.tmpdir, "a.py")
        path.write_bytes(b"old\r\n")
        result = self.edit("a.py", "old\n", "new\n")
        self.assertTrue(result.ok)
        self.assertIn("Checkpoint: cp-", result.text)

        restored = self.manager.restore()
        self.assertEqual(restored.path, "a.py")
        self.assertEqual(path.read_bytes(), b"old\r\n")

    def test_agent_created_file_is_deleted_on_restore(self):
        path = Path(self.tmpdir, "new.txt")
        self.assertTrue(self.write("new.txt", "created").ok)
        self.assertTrue(path.exists())
        restored = self.manager.restore()
        self.assertTrue(restored.deleted_created_file)
        self.assertFalse(path.exists())

    def test_later_user_change_causes_conflict_instead_of_overwrite(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("old", encoding="utf-8")
        self.assertTrue(self.edit("a.txt", "old", "agent").ok)
        path.write_text("user-later", encoding="utf-8")

        with self.assertRaisesRegex(CheckpointError, "changed after the Agent edit"):
            self.manager.restore()
        self.assertEqual(path.read_text(encoding="utf-8"), "user-later")

    def test_multiple_edits_undo_in_lifo_order(self):
        path = Path(self.tmpdir, "a.txt")
        path.write_text("v0", encoding="utf-8")
        self.edit("a.txt", "v0", "v1")
        self.edit("a.txt", "v1", "v2")

        second = self.manager.restore()
        self.assertEqual(path.read_text(encoding="utf-8"), "v1")
        first = self.manager.restore()
        self.assertEqual(path.read_text(encoding="utf-8"), "v0")
        self.assertNotEqual(first.checkpoint_id, second.checkpoint_id)
        self.assertEqual(self.manager.active(), [])

    def test_restore_since_restores_all_task_files_after_preflight(self):
        first = Path(self.tmpdir, "a.txt")
        second = Path(self.tmpdir, "b.txt")
        first.write_text("a0", encoding="utf-8")
        second.write_text("b0", encoding="utf-8")
        self.edit("a.txt", "a0", "a1")
        self.edit("b.txt", "b0", "b1")

        restored = self.manager.restore_since(0)

        self.assertEqual(len(restored), 2)
        self.assertEqual(first.read_text(encoding="utf-8"), "a0")
        self.assertEqual(second.read_text(encoding="utf-8"), "b0")

    def test_restore_since_conflict_does_not_partially_restore_other_files(self):
        first = Path(self.tmpdir, "a.txt")
        second = Path(self.tmpdir, "b.txt")
        first.write_text("a0", encoding="utf-8")
        second.write_text("b0", encoding="utf-8")
        self.edit("a.txt", "a0", "a1")
        self.edit("b.txt", "b0", "b1")
        first.write_text("user", encoding="utf-8")

        with self.assertRaisesRegex(CheckpointError, "changed after the latest Agent edit"):
            self.manager.restore_since(0)

        self.assertEqual(first.read_text(encoding="utf-8"), "user")
        self.assertEqual(second.read_text(encoding="utf-8"), "b1")

    def test_active_checkpoints_report_one_truthful_net_line_summary_per_file(self):
        target = Path(self.tmpdir, "summary.py")
        target.write_text("a\nb\n", encoding="utf-8")
        first_after = b"a\nc\nd\n"
        first = self.manager.prepare("summary.py", b"a\nb\n", first_after, 0)
        target.write_bytes(first_after)
        self.manager.commit(first, 1)
        second_after = b"a\nc\ne\n"
        second = self.manager.prepare("summary.py", first_after, second_after, 1)
        target.write_bytes(second_after)
        self.manager.commit(second, 2)

        summaries = self.manager.change_summaries()

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].path, "summary.py")
        self.assertEqual(summaries[0].kind, "modified")
        self.assertEqual((summaries[0].additions, summaries[0].deletions), (2, 1))

    def test_change_summary_can_start_at_current_turn_checkpoint(self):
        first_path = Path(self.tmpdir, "previous.py")
        first_path.write_text("old\n", encoding="utf-8")
        first = self.manager.prepare("previous.py", b"old\n", b"prior\n", 0)
        first_path.write_bytes(b"prior\n")
        self.manager.commit(first, 1)
        cursor = len(self.manager.active())

        current_path = Path(self.tmpdir, "current.py")
        current_path.write_text("before\n", encoding="utf-8")
        current = self.manager.prepare("current.py", b"before\n", b"after\n", 1)
        current_path.write_bytes(b"after\n")
        self.manager.commit(current, 2)

        summaries = self.manager.change_summaries(cursor)

        self.assertEqual([change.path for change in summaries], ["current.py"])

    def test_file_diff_uses_checkpoint_before_image_and_current_hash(self):
        target = Path(self.tmpdir, "service.py")
        target.write_text("value = 1\n", encoding="utf-8")
        self.assertTrue(self.edit("service.py", "value = 1", "value = 2").ok)

        diff = self.manager.file_diff("service.py")

        self.assertEqual(diff.kind, "modified")
        self.assertIn("-value = 1", diff.text)
        self.assertIn("+value = 2", diff.text)

        target.write_text("user = 3\n", encoding="utf-8")
        with self.assertRaisesRegex(CheckpointError, "trusted diff"):
            self.manager.file_diff("service.py")


if __name__ == "__main__":
    unittest.main()
