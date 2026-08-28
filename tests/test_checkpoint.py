from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkpoint import CheckpointError, CheckpointManager  # noqa: E402
import config  # noqa: E402
from state import State  # noqa: E402
import tools  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
