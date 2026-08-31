from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from zxn_agent.changes import unified_byte_diff
from zxn_agent.gui_data import (
    RecentWorkspaceStore,
    WorkspaceDataError,
    project_entries,
    project_files,
    read_workspace_text,
    switch_workspace,
)


class TestGuiData(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recent_store_only_contains_explicit_existing_workspaces(self):
        first = self.tmpdir / "first"
        second = self.tmpdir / "second"
        first.mkdir()
        second.mkdir()
        store = RecentWorkspaceStore(self.tmpdir / "gui.json", limit=2)

        store.remember(first)
        recent = store.remember(second)

        self.assertEqual(recent, [str(second.resolve()), str(first.resolve())])
        first.rmdir()
        self.assertEqual(store.load(), [str(second.resolve())])

    def test_workspace_switch_rejects_running_and_missing_paths(self):
        with self.assertRaisesRegex(WorkspaceDataError, "before switching"):
            switch_workspace(self.tmpdir, running=True)
        with self.assertRaisesRegex(WorkspaceDataError, "does not exist"):
            switch_workspace(self.tmpdir / "missing", running=False)
        self.assertEqual(switch_workspace(self.tmpdir, running=False), self.tmpdir.resolve())

    def test_project_files_and_preview_stay_inside_workspace(self):
        (self.tmpdir / "src").mkdir()
        (self.tmpdir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
        (self.tmpdir / ".agent").mkdir()
        (self.tmpdir / ".agent" / "secret.jsonl").write_text("private", encoding="utf-8")

        self.assertEqual(project_files(self.tmpdir), ["src/app.py"])
        text, truncated = read_workspace_text(self.tmpdir, "src/app.py")
        self.assertIn("print", text)
        self.assertFalse(truncated)
        with self.assertRaisesRegex(WorkspaceDataError, "escapes"):
            read_workspace_text(self.tmpdir, "../outside.txt")

    def test_project_entries_include_empty_directories_and_hide_runtime_noise(self):
        (self.tmpdir / "empty-demo").mkdir()
        (self.tmpdir / "src").mkdir()
        (self.tmpdir / "src" / "app.py").write_text("pass\n", encoding="utf-8")
        (self.tmpdir / ".agent").mkdir()

        entries = [(entry.path, entry.is_directory) for entry in project_entries(self.tmpdir)]

        self.assertIn(("empty-demo", True), entries)
        self.assertIn(("src", True), entries)
        self.assertIn(("src/app.py", False), entries)
        self.assertNotIn((".agent", True), entries)

    def test_real_diff_supports_added_modified_deleted_and_bounded_output(self):
        added = unified_byte_diff("new.py", None, b"a\nb\n")
        modified = unified_byte_diff("app.py", b"a\n", b"b\n")
        deleted = unified_byte_diff("old.py", b"a\n", None)
        bounded = unified_byte_diff(
            "large.py",
            b"old\n" * 200,
            b"new\n" * 200,
            max_chars=300,
        )

        self.assertEqual([added.kind, modified.kind, deleted.kind], ["added", "modified", "deleted"])
        self.assertIn("+a", added.text)
        self.assertIn("-a", deleted.text)
        self.assertTrue(bounded.truncated)
        self.assertLessEqual(len(bounded.text), 300)


if __name__ == "__main__":
    unittest.main()
