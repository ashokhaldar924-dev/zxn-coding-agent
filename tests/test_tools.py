from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from gitguard import GitGuard  # noqa: E402
from state import State  # noqa: E402
import tools  # noqa: E402


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_workspace = config.WORKSPACE_DIR
        self.old_confirm = config.REQUIRE_CONFIRMATION
        self.old_limit = config.MAX_TOOL_CHARS
        self.old_timeout = config.CMD_TIMEOUT
        config.WORKSPACE_DIR = self.tmpdir
        config.REQUIRE_CONFIRMATION = False
        config.MAX_TOOL_CHARS = 12_000
        config.CMD_TIMEOUT = 2
        self.st = State()

    def tearDown(self):
        config.WORKSPACE_DIR = self.old_workspace
        config.REQUIRE_CONFIRMATION = self.old_confirm
        config.MAX_TOOL_CHARS = self.old_limit
        config.CMD_TIMEOUT = self.old_timeout
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_tool(self, name, args):
        return tools.run_tool(name, args, self.st)

    def test_registry_has_exactly_seven_tools(self):
        self.assertEqual(
            list(tools.REG),
            ["read_file", "write_file", "edit_file", "list_dir", "search_text", "run_command", "check_command"],
        )

    def test_safe_path_and_traversal(self):
        self.assertEqual(tools._resolve_safe_path("a.txt"), Path(self.tmpdir) / "a.txt")
        result = self.run_tool("read_file", {"path": "../outside.txt"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.text)

    def test_read_range_and_200_line_cap(self):
        Path(self.tmpdir, "lines.txt").write_text(
            "\n".join(f"line {i}" for i in range(1, 251)), encoding="utf-8"
        )
        ranged = self.run_tool("read_file", {"path": "lines.txt", "start": 11, "end": 13})
        self.assertTrue(ranged.ok)
        self.assertIn("lines.txt 11-13 / 250", ranged.text)
        self.assertIn("11 | line 11", ranged.text)
        capped = self.run_tool("read_file", {"path": "lines.txt", "start": 1, "end": 250})
        self.assertIn("1-200 / 250", capped.text)
        self.assertIn("capped at 200", capped.text)
        self.assertNotIn("201 |", capped.text)

    def test_write_create_noop_and_revision(self):
        with contextlib.redirect_stdout(io.StringIO()):
            first = self.run_tool("write_file", {"path": "sub/a.txt", "content": "one\n"})
            noop = self.run_tool("write_file", {"path": "sub/a.txt", "content": "one\n"})
        self.assertTrue(first.ok)
        self.assertEqual(self.st.rev, 1)
        self.assertTrue(self.st.changed)
        self.assertEqual(self.st.files, {"sub/a.txt"})
        self.assertIn("No change", noop.text)
        self.assertEqual(self.st.rev, 1)

    def test_write_diff_preview_and_rejection(self):
        config.REQUIRE_CONFIRMATION = True
        stream = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(stream):
            result = self.run_tool("write_file", {"path": "blocked.txt", "content": "x\n"})
        self.assertTrue(result.ok)
        self.assertTrue(result.rejected)
        self.assertIn("+x", stream.getvalue())
        self.assertFalse(Path(self.tmpdir, "blocked.txt").exists())
        self.assertEqual(self.st.errs, 0)

    def test_edit_zero_one_multiple_and_empty(self):
        Path(self.tmpdir, "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        zero = self.run_tool("edit_file", {"path": "a.txt", "old": "missing", "new": "x"})
        empty = self.run_tool("edit_file", {"path": "a.txt", "old": "", "new": "x"})
        Path(self.tmpdir, "a.txt").write_text("same same", encoding="utf-8")
        multiple = self.run_tool("edit_file", {"path": "a.txt", "old": "same", "new": "x"})
        Path(self.tmpdir, "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            one = self.run_tool("edit_file", {"path": "a.txt", "old": "beta", "new": "gamma"})
        self.assertFalse(zero.ok)
        self.assertFalse(empty.ok)
        self.assertFalse(multiple.ok)
        self.assertTrue(one.ok)
        self.assertEqual(Path(self.tmpdir, "a.txt").read_text(encoding="utf-8"), "alpha\ngamma\n")
        self.assertEqual(self.st.rev, 1)

    def test_edit_preserves_crlf_and_noop_ignores_newline_style(self):
        Path(self.tmpdir, "win.txt").write_bytes(b"alpha\r\nbeta\r\n")
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.run_tool(
                "edit_file",
                {"path": "win.txt", "old": "alpha\nbeta", "new": "alpha\ngamma"},
            )
        self.assertTrue(result.ok)
        self.assertEqual(Path(self.tmpdir, "win.txt").read_bytes(), b"alpha\r\ngamma\r\n")
        with contextlib.redirect_stdout(io.StringIO()):
            noop = self.run_tool(
                "write_file", {"path": "win.txt", "content": "alpha\ngamma\n"}
            )
        self.assertIn("No change", noop.text)
        self.assertEqual(self.st.rev, 1)

    def test_edit_rejection_does_not_change_revision(self):
        Path(self.tmpdir, "a.txt").write_text("old", encoding="utf-8")
        config.REQUIRE_CONFIRMATION = True
        with mock.patch("builtins.input", return_value="no"), contextlib.redirect_stdout(io.StringIO()):
            result = self.run_tool("edit_file", {"path": "a.txt", "old": "old", "new": "new"})
        self.assertTrue(result.rejected)
        self.assertEqual(Path(self.tmpdir, "a.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual(self.st.rev, 0)

    def test_initially_dirty_file_requires_its_own_approval(self):
        path = Path(self.tmpdir, "dirty.py")
        path.write_text("old", encoding="utf-8")
        self.st.git_guard = GitGuard(
            repo_root=Path(self.tmpdir),
            initial_dirty={path.resolve()},
        )
        self.st.permissions.allow_clean_edits = True
        config.REQUIRE_CONFIRMATION = True
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            result = self.run_tool(
                "edit_file",
                {"path": "dirty.py", "old": "old", "new": "new"},
            )
        self.assertTrue(result.rejected)
        self.assertEqual(path.read_text(encoding="utf-8"), "old")

    def test_list_dir_skips_noise(self):
        Path(self.tmpdir, "visible.txt").write_text("x", encoding="utf-8")
        Path(self.tmpdir, ".git").mkdir()
        Path(self.tmpdir, "src").mkdir()
        result = self.run_tool("list_dir", {})
        self.assertIn("visible.txt", result.text)
        self.assertIn("[DIR] src", result.text)
        self.assertNotIn(".git", result.text)

    def test_empty_optional_directory_path_means_workspace(self):
        Path(self.tmpdir, "visible.txt").write_text("needle", encoding="utf-8")
        listed = self.run_tool("list_dir", {"path": ""})
        searched = self.run_tool("search_text", {"query": "needle", "path": ""})
        self.assertTrue(listed.ok)
        self.assertIn("visible.txt", listed.text)
        self.assertTrue(searched.ok)
        self.assertIn("visible.txt:1", searched.text)

    def test_search_literal_and_30_result_cap(self):
        Path(self.tmpdir, "many.txt").write_text(
            "\n".join(f"needle {i}" for i in range(40)), encoding="utf-8"
        )
        result = self.run_tool("search_text", {"query": "needle"})
        self.assertTrue(result.ok)
        self.assertIn("Found 40 matches; showing first 30", result.text)
        self.assertEqual(sum(": needle " in line for line in result.text.splitlines()), 30)

    def test_run_success_nonzero_and_rejection(self):
        success = self.run_tool("run_command", {"cmd": f'"{sys.executable}" -c "print(123)"'})
        nonzero = self.run_tool("run_command", {"cmd": f'"{sys.executable}" -c "raise SystemExit(7)"'})
        self.assertTrue(success.ok)
        self.assertEqual(success.rc, 0)
        self.assertIn("123", success.text)
        self.assertTrue(nonzero.ok)
        self.assertEqual(nonzero.rc, 7)
        config.REQUIRE_CONFIRMATION = True
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            rejected = self.run_tool("run_command", {"cmd": "echo no"})
        self.assertTrue(rejected.ok)
        self.assertTrue(rejected.rejected)

    def test_timeout_is_runtime_error(self):
        result = self.run_tool(
            "run_command",
            {"cmd": f'"{sys.executable}" -c "import time; time.sleep(1)"', "timeout": 0.1},
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.rc)
        self.assertIn("timed out", result.text)

    def test_head_tail_truncation(self):
        config.MAX_TOOL_CHARS = 120
        result = self.run_tool(
            "run_command",
            {"cmd": f'"{sys.executable}" -c "print(\'A\'*100); print(\'TAIL\')"'},
        )
        self.assertIn("output truncated", result.text)
        self.assertIn("TAIL", result.text)
        self.assertLessEqual(len(result.text), 120)

    def test_check_updates_only_on_success(self):
        self.st.rev = 3
        failed = self.run_tool("check_command", {"cmd": f'"{sys.executable}" -c "raise SystemExit(1)"'})
        self.assertTrue(failed.ok)
        self.assertEqual(self.st.ok_rev, -1)
        passed = self.run_tool("check_command", {"cmd": f'"{sys.executable}" -c "print(\'ok\')"'})
        self.assertEqual(passed.rc, 0)
        self.assertEqual(self.st.ok_rev, 3)
        config.REQUIRE_CONFIRMATION = True
        self.st.rev = 4
        with mock.patch("builtins.input", return_value="n"), contextlib.redirect_stdout(io.StringIO()):
            rejected = self.run_tool("check_command", {"cmd": "echo no"})
        self.assertTrue(rejected.rejected)
        self.assertEqual(self.st.ok_rev, 3)


if __name__ == "__main__":
    unittest.main()
