from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import tools
from gitguard import GitGuard
from state import State


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_workspace = config.WORKSPACE_DIR
        self.old_confirm = config.REQUIRE_CONFIRMATION
        self.old_permission_mode = config.PERMISSION_MODE
        self.old_limit = config.MAX_TOOL_CHARS
        self.old_timeout = config.CMD_TIMEOUT
        config.WORKSPACE_DIR = self.tmpdir
        config.REQUIRE_CONFIRMATION = False
        config.PERMISSION_MODE = "balanced"
        config.MAX_TOOL_CHARS = 12_000
        config.CMD_TIMEOUT = 2
        self.st = State()

    def tearDown(self):
        config.WORKSPACE_DIR = self.old_workspace
        config.REQUIRE_CONFIRMATION = self.old_confirm
        config.PERMISSION_MODE = self.old_permission_mode
        config.MAX_TOOL_CHARS = self.old_limit
        config.CMD_TIMEOUT = self.old_timeout
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_user_stop_terminates_running_command(self):
        cancel = threading.Event()
        st = State(cancel_event=cancel)
        st.initialize_workspace_tracking(self.tmpdir)
        timer = threading.Timer(0.15, cancel.set)
        command = f'"{sys.executable}" -c "import time; time.sleep(30)"'

        timer.start()
        started = time.monotonic()
        try:
            result = tools.run_user_command(command, timeout=10, st=st)
        finally:
            timer.cancel()

        self.assertTrue(result.cancelled)
        self.assertTrue(result.blocked)
        self.assertEqual(result.block_kind, "user_stopped")
        self.assertIn("cancelled by user", result.text.lower())
        self.assertLess(time.monotonic() - started, 5)

    def run_tool(self, name, args):
        return tools.run_tool(name, args, self.st)

    def test_registry_has_exactly_twelve_tools(self):
        self.assertEqual(
            list(tools.REG),
            [
                "read_file",
                "read_command_output",
                "write_file",
                "edit_file",
                "multi_edit",
                "list_dir",
                "glob_files",
                "repo_map",
                "search_text",
                "update_plan",
                "run_command",
                "check_command",
            ],
        )

    def test_safe_path_and_traversal(self):
        self.assertEqual(tools._resolve_safe_path("a.txt"), Path(self.tmpdir) / "a.txt")
        result = self.run_tool("read_file", {"path": "../outside.txt"})
        private = self.run_tool("read_file", {"path": ".agent/session.jsonl"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.text)
        self.assertFalse(private.ok)
        self.assertIn("private .agent", private.text)

    def test_read_range_cap_and_unchanged_short_observation(self):
        Path(self.tmpdir, "lines.txt").write_text(
            "\n".join(f"line {i}" for i in range(1, 251)), encoding="utf-8"
        )
        ranged = self.run_tool("read_file", {"path": "lines.txt", "start": 11, "end": 13})
        self.assertTrue(ranged.ok)
        self.assertIn("lines.txt 11-13 / 250", ranged.text)
        self.assertIn("11 | line 11", ranged.text)
        repeated = self.run_tool("read_file", {"path": "lines.txt", "start": 11, "end": 13})
        self.assertIn("unchanged", repeated.text)
        self.assertNotIn("11 | line 11", repeated.text)
        for step in range(1, config.MAX_GROUPS + 1):
            self.st.step = step
            still_recent = self.run_tool(
                "read_file", {"path": "lines.txt", "start": 11, "end": 13}
            )
            self.assertIn("unchanged", still_recent.text)
        self.st.step = config.MAX_GROUPS + 1
        expired = self.run_tool("read_file", {"path": "lines.txt", "start": 11, "end": 13})
        self.assertIn("11 | line 11", expired.text)
        self.assertNotIn("unchanged", expired.text)
        path = Path(self.tmpdir, "lines.txt")
        path.write_text(
            path.read_text(encoding="utf-8").replace("line 12", "changed 12"),
            encoding="utf-8",
        )
        refreshed = self.run_tool("read_file", {"path": "lines.txt", "start": 11, "end": 13})
        self.assertIn("12 | changed 12", refreshed.text)
        self.assertNotIn("unchanged", refreshed.text)
        capped = self.run_tool("read_file", {"path": "lines.txt", "start": 1, "end": 250})
        self.assertIn("1-200 / 250", capped.text)
        self.assertIn("capped at 200", capped.text)
        self.assertNotIn("201 |", capped.text)

    def test_read_truncates_one_huge_line_without_losing_continuation_metadata(self):
        Path(self.tmpdir, "minified.js").write_text(
            "const payload = '" + "A" * 8_000 + "'; // tail-marker\nnext();\n",
            encoding="utf-8",
        )

        result = self.run_tool("read_file", {"path": "minified.js"})

        self.assertTrue(result.ok)
        self.assertIn("line truncated: 8034 chars", result.text)
        self.assertIn("tail-marker", result.text)
        self.assertIn("2 | next();", result.text)
        self.assertLessEqual(len(result.text), config.MAX_TOOL_CHARS)

    def test_write_create_noop_and_revision(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            first = self.run_tool("write_file", {"path": "sub/a.txt", "content": "one\n"})
            noop = self.run_tool("write_file", {"path": "sub/a.txt", "content": "one\n"})
        self.assertTrue(first.ok)
        self.assertEqual(self.st.rev, 1)
        self.assertTrue(self.st.changed)
        self.assertEqual(self.st.files, {"sub/a.txt"})
        self.assertIn("No change", noop.text)
        self.assertEqual(self.st.rev, 1)
        self.assertEqual(
            (first.file_changes[0].kind, first.file_changes[0].additions),
            ("added", 1),
        )
        self.assertNotIn("+one", stream.getvalue())

    def test_write_diff_preview_and_rejection(self):
        config.REQUIRE_CONFIRMATION = True
        config.PERMISSION_MODE = "manual"
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
        Path(self.tmpdir, "multiple.txt").write_text("same same", encoding="utf-8")
        zero = self.run_tool("edit_file", {"path": "a.txt", "old": "missing", "new": "x"})
        empty = self.run_tool("edit_file", {"path": "a.txt", "old": "", "new": "x"})
        multiple = self.run_tool(
            "edit_file", {"path": "multiple.txt", "old": "same", "new": "x"}
        )
        with contextlib.redirect_stdout(io.StringIO()):
            one = self.run_tool("edit_file", {"path": "a.txt", "old": "beta", "new": "gamma"})
        self.assertFalse(zero.ok)
        self.assertFalse(empty.ok)
        self.assertFalse(multiple.ok)
        self.assertTrue(one.ok)
        self.assertEqual(Path(self.tmpdir, "a.txt").read_text(encoding="utf-8"), "alpha\ngamma\n")
        self.assertEqual(self.st.rev, 1)

    def test_multi_edit_is_atomic_exact_and_preserves_bom_and_crlf(self):
        path = Path(self.tmpdir, "batch.py")
        path.write_bytes(tools.UTF8_BOM + b"one\r\ntwo\r\n")
        failed = self.run_tool(
            "multi_edit",
            {
                "path": "batch.py",
                "edits": [
                    {"old": "one", "new": "first"},
                    {"old": "missing", "new": "second"},
                ],
            },
        )
        self.assertFalse(failed.ok)
        self.assertEqual(path.read_bytes(), tools.UTF8_BOM + b"one\r\ntwo\r\n")
        self.assertEqual(self.st.rev, 0)

        with contextlib.redirect_stdout(io.StringIO()):
            changed = self.run_tool(
                "multi_edit",
                {
                    "path": "batch.py",
                    "edits": [
                        {"old": "one", "new": "first"},
                        {"old": "two", "new": "second"},
                    ],
                },
            )
        self.assertTrue(changed.ok)
        self.assertEqual(path.read_bytes(), tools.UTF8_BOM + b"first\r\nsecond\r\n")
        self.assertEqual(self.st.rev, 1)
        self.assertIn("2 exact edits", changed.text)

    def test_atomic_replace_failure_preserves_original_file(self):
        path = Path(self.tmpdir, "atomic.txt")
        path.write_text("old\n", encoding="utf-8")
        with mock.patch.object(
            tools.os, "replace", side_effect=OSError("locked")
        ), contextlib.redirect_stdout(io.StringIO()):
            result = self.run_tool(
                "edit_file",
                {"path": "atomic.txt", "old": "old", "new": "new"},
            )
        self.assertFalse(result.ok)
        self.assertIn("locked", result.text)
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(self.st.rev, 0)

    def test_external_edit_is_not_overwritten_until_the_file_is_read_again(self):
        path = Path(self.tmpdir, "shared.txt")
        path.write_text("original\n", encoding="utf-8")
        first_read = self.run_tool("read_file", {"path": "shared.txt"})
        self.assertTrue(first_read.ok)

        path.write_text("user version\n", encoding="utf-8")
        blocked = self.run_tool(
            "edit_file",
            {"path": "shared.txt", "old": "user version", "new": "agent version"},
        )

        self.assertTrue(blocked.blocked)
        self.assertEqual(blocked.block_kind, "external_change")
        self.assertEqual(path.read_text(encoding="utf-8"), "user version\n")
        self.assertEqual(self.st.rev, 1)
        self.assertEqual(self.st.files, {"shared.txt"})

        refreshed = self.run_tool("read_file", {"path": "shared.txt"})
        self.assertIn("user version", refreshed.text)
        with contextlib.redirect_stdout(io.StringIO()):
            updated = self.run_tool(
                "edit_file",
                {"path": "shared.txt", "old": "user version", "new": "agent version"},
            )
        self.assertTrue(updated.ok)
        self.assertFalse(updated.blocked)
        self.assertEqual(path.read_text(encoding="utf-8"), "agent version\n")
        self.assertEqual(self.st.rev, 2)

        path.unlink()
        deleted = self.run_tool(
            "edit_file", {"path": "shared.txt", "old": "agent", "new": "replacement"}
        )
        self.assertTrue(deleted.blocked)
        self.assertEqual(deleted.block_kind, "external_change")
        self.assertFalse(path.exists())
        self.assertEqual(self.st.rev, 3)

        resume_path = Path(self.tmpdir, "resume.txt")
        resume_path.write_text("changed while stopped\n", encoding="utf-8")
        resumed = State()
        resumed.initialize_workspace_tracking(
            self.tmpdir,
            require_file_observation=True,
        )
        before_read = tools.run_tool(
            "edit_file",
            {"path": "resume.txt", "old": "changed", "new": "overwritten"},
            resumed,
        )
        self.assertTrue(before_read.blocked)
        self.assertEqual(resumed.rev, 0)
        tools.run_tool("read_file", {"path": "resume.txt"}, resumed)
        with contextlib.redirect_stdout(io.StringIO()):
            after_read = tools.run_tool(
                "edit_file",
                {"path": "resume.txt", "old": "changed", "new": "accepted"},
                resumed,
            )
        self.assertTrue(after_read.ok)
        self.assertEqual(resume_path.read_text(encoding="utf-8"), "accepted while stopped\n")

    def test_edit_rechecks_exact_bytes_after_permission_before_writing(self):
        path = Path(self.tmpdir, "race.txt")
        path.write_text("old\n", encoding="utf-8")
        original_authorize = self.st.permissions.authorize_edit

        def user_edits_during_approval(relative, *, initially_dirty=False):
            path.write_text("human edit\n", encoding="utf-8")
            return original_authorize(relative, initially_dirty=initially_dirty)

        with mock.patch.object(
            self.st.permissions,
            "authorize_edit",
            side_effect=user_edits_during_approval,
        ), contextlib.redirect_stdout(io.StringIO()):
            result = self.run_tool(
                "edit_file", {"path": "race.txt", "old": "old", "new": "agent"}
            )

        self.assertTrue(result.blocked)
        self.assertEqual(result.block_kind, "external_change")
        self.assertIn("after the proposed diff was prepared", result.text)
        self.assertEqual(path.read_text(encoding="utf-8"), "human edit\n")
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
        config.PERMISSION_MODE = "manual"
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

    def test_list_search_and_glob_pages_have_explicit_next_offsets(self):
        for index in range(5):
            Path(self.tmpdir, f"page-{index}.txt").write_text(
                f"needle {index}\n", encoding="utf-8"
            )

        listed = self.run_tool("list_dir", {"offset": 1, "limit": 2})
        searched = self.run_tool(
            "search_text", {"query": "needle", "offset": 2, "limit": 2}
        )
        globbed = self.run_tool(
            "glob_files", {"pattern": "*.txt", "offset": 3, "limit": 1}
        )

        self.assertIn("page-1.txt", listed.text)
        self.assertNotIn("page-0.txt", listed.text)
        self.assertIn("Found 5 entries; showing 2-3. next offset: 3.", listed.text)
        self.assertIn("page-2.txt:1: needle 2", searched.text)
        self.assertIn("Found 5 matches; showing 3-4. next offset: 4.", searched.text)
        self.assertIn("page-3.txt", globbed.text)
        self.assertIn("Found 5 files; showing 4-4. next offset: 4.", globbed.text)

    def test_search_regex_and_invalid_pattern(self):
        Path(self.tmpdir, "code.py").write_text(
            "class FirstHandler:\n    pass\nclass Other:\n    pass\n",
            encoding="utf-8",
        )
        result = self.run_tool(
            "search_text",
            {"query": r"^class .*Handler:", "regex": True},
        )
        invalid = self.run_tool(
            "search_text",
            {"query": "(", "regex": True},
        )
        self.assertTrue(result.ok)
        self.assertIn("code.py:1: class FirstHandler:", result.text)
        self.assertFalse(invalid.ok)
        self.assertIn("invalid regular expression", invalid.text)

    def test_glob_files_is_bounded_and_rejects_parent_patterns(self):
        Path(self.tmpdir, "root.py").write_text("", encoding="utf-8")
        Path(self.tmpdir, "src").mkdir()
        Path(self.tmpdir, "src", "nested.py").write_text("", encoding="utf-8")
        Path(self.tmpdir, "src", "note.txt").write_text("", encoding="utf-8")
        Path(self.tmpdir, ".git").mkdir()
        Path(self.tmpdir, ".git", "hidden.py").write_text("", encoding="utf-8")
        result = self.run_tool("glob_files", {"pattern": "**/*.py"})
        escaped = self.run_tool("glob_files", {"pattern": "../*.py"})
        self.assertTrue(result.ok)
        self.assertEqual(result.text.splitlines(), ["root.py", "src/nested.py"])
        self.assertFalse(escaped.ok)
        self.assertIn("must stay relative", escaped.text)

    def test_repo_map_extracts_python_classes_functions_methods_and_lines(self):
        Path(self.tmpdir, "module.py").write_text(
            "def top(a, b):\n"
            "    return a + b\n\n"
            "class Worker(Base):\n"
            "    async def run(self, item):\n"
            "        return item\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "broken.py").write_text("def broken(:\n", encoding="utf-8")
        result = self.run_tool("repo_map", {"path": "."})
        self.assertTrue(result.ok)
        self.assertIn("module.py", result.text)
        self.assertIn("L1 def top(a, b)", result.text)
        self.assertIn("L4 class Worker(Base)", result.text)
        self.assertIn("L5 async def run(self, item)", result.text)
        self.assertIn("could not be parsed", result.text)

    def test_repo_map_extracts_ranked_multilanguage_declarations_with_lines(self):
        Path(self.tmpdir, "index.ts").write_text(
            "export interface User { id: number }\n"
            "export async function loadUser(id: number) { return id }\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "main.go").write_text(
            "package main\nfunc Run() {}\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "lib.rs").write_text(
            "pub struct Store {}\npub fn open() {}\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "Main.java").write_text(
            "public class Main {}\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "worker.cpp").write_text(
            "class Worker {};\nint execute() { return 0; }\n",
            encoding="utf-8",
        )

        result = self.run_tool("repo_map", {"path": "."})

        self.assertTrue(result.ok)
        self.assertIn("Languages: C++, Go, Java, Rust, TypeScript", result.text)
        self.assertIn("L1 export interface User", result.text)
        self.assertIn("L2 func Run()", result.text)
        self.assertIn("L1 pub struct Store", result.text)
        self.assertIn("L1 public class Main", result.text)
        self.assertIn("L2 int execute()", result.text)

    def test_repo_map_does_not_follow_file_symlinks_outside_workspace(self):
        outside_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside_dir, True)
        outside = Path(outside_dir, "secret.py")
        outside.write_text("def outside_secret():\n    pass\n", encoding="utf-8")
        link = Path(self.tmpdir, "link.py")
        try:
            link.symlink_to(outside)
        except OSError:
            # Windows may deny symlink creation. Feeding the resolved outside
            # candidate exercises the same per-candidate boundary check.
            with mock.patch.object(tools, "_search_files", return_value=iter([outside])):
                result = self.run_tool("repo_map", {"path": "."})
        else:
            result = self.run_tool("repo_map", {"path": "."})

        self.assertTrue(result.ok)
        self.assertNotIn("outside_secret", result.text)
        self.assertNotIn("link.py", result.text)

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
            rejected = self.run_tool("run_command", {"cmd": "custom-tool action"})
        self.assertTrue(rejected.ok)
        self.assertTrue(rejected.rejected)

    def test_policy_denied_command_is_blocked_not_runtime_error(self):
        result = self.run_tool("run_command", {"cmd": "git reset --hard"})
        self.assertTrue(result.ok)
        self.assertTrue(result.blocked)
        self.assertEqual(result.block_kind, "permission")
        self.assertIsNone(result.rc)

    def test_timeout_is_runtime_error(self):
        result = self.run_tool(
            "run_command",
            {"cmd": f'"{sys.executable}" -c "import time; time.sleep(1)"', "timeout": 0.1},
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.rc)
        self.assertIn("timed out", result.text)

    def test_timeout_terminates_an_ordinary_child_process_tree(self):
        Path(self.tmpdir, "child.py").write_text(
            "import time\n"
            "from pathlib import Path\n"
            "time.sleep(1)\n"
            "Path('survivor.txt').write_text('alive', encoding='utf-8')\n",
            encoding="utf-8",
        )
        Path(self.tmpdir, "parent.py").write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, 'child.py'])\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )

        result = self.run_tool(
            "run_command",
            {"cmd": f'"{sys.executable}" parent.py', "timeout": 0.5},
        )
        time.sleep(1.2)

        self.assertFalse(result.ok)
        self.assertIn("timed out", result.text)
        self.assertFalse(Path(self.tmpdir, "survivor.txt").exists())

    def test_command_does_not_inherit_the_agent_api_key(self):
        old_key = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_API_KEY"] = "model-credential-must-not-reach-command"
        try:
            result = self.run_tool(
                "run_command",
                {
                    "cmd": (
                        f'"{sys.executable}" -c "import os; '
                        "print(os.getenv('AGENT_API_KEY', 'MISSING'))\""
                    )
                },
            )
        finally:
            if old_key is None:
                os.environ.pop("AGENT_API_KEY", None)
            else:
                os.environ["AGENT_API_KEY"] = old_key

        self.assertEqual(result.rc, 0)
        self.assertIn("MISSING", result.text)
        self.assertNotIn("[REDACTED]", result.text)

    def test_long_command_output_is_saved_and_can_be_read_by_range(self):
        config.MAX_TOOL_CHARS = 180
        result = self.run_tool(
            "run_command",
            {
                "cmd": (
                    f'"{sys.executable}" -c "print(\'A\'*1000000, end=\'\'); '
                    "print('TAIL')\""
                )
            },
        )
        self.assertIn("output truncated", result.text)
        self.assertIn("TAIL", result.text)
        self.assertLessEqual(len(result.text), 180)
        self.assertIsNotNone(result.output_ref)
        saved = Path(
            self.tmpdir,
            ".agent",
            "outputs",
            "runtime",
            result.output_ref,
        )
        self.assertTrue(saved.is_file())
        self.assertGreater(saved.stat().st_size, 1_000_000)

        reread = self.run_tool(
            "read_command_output",
            {"output_id": result.output_ref, "offset": 0, "limit": 120},
        )
        self.assertTrue(reread.ok)
        self.assertIn(result.output_ref, reread.text)
        self.assertIn("exit code: 0", reread.text)
        tail = self.run_tool(
            "read_command_output",
            {
                "output_id": result.output_ref,
                "offset": result.output_chars - 20,
                "limit": 20,
            },
        )
        self.assertIn("TAIL", tail.text)
        self.assertIn("end of saved output", tail.text)

        old_key = os.environ.get("AGENT_API_KEY")
        os.environ["AGENT_API_KEY"] = "command-output-test-secret"
        try:
            config.MAX_TOOL_CHARS = 80
            secret_result = self.run_tool(
                "run_command",
                {
                    "cmd": f'"{sys.executable}" -c "print(\'command-output-test-secret\' * 20)"'
                },
            )
            secret_path = Path(
                self.tmpdir,
                ".agent",
                "outputs",
                "runtime",
                secret_result.output_ref,
            )
            self.assertNotIn("command-output-test-secret", secret_result.text)
            self.assertNotIn(
                "command-output-test-secret",
                secret_path.read_text(encoding="utf-8"),
            )
        finally:
            if old_key is None:
                os.environ.pop("AGENT_API_KEY", None)
            else:
                os.environ["AGENT_API_KEY"] = old_key

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
            rejected = self.run_tool("check_command", {"cmd": "custom-tool check"})
        self.assertTrue(rejected.rejected)
        self.assertEqual(self.st.ok_rev, 3)

    def test_check_that_changes_workspace_requires_a_stable_rerun(self):
        command = (
            f'"{sys.executable}" -c "from pathlib import Path; '
            "Path('generated.txt').write_text('stable', encoding='utf-8')\""
        )

        first = self.run_tool("check_command", {"cmd": command})

        self.assertEqual(first.rc, 0)
        self.assertEqual(self.st.rev, 1)
        self.assertEqual(self.st.ok_rev, -1)
        self.assertEqual(first.changed_files, ["generated.txt"])
        self.assertIn("did not verify the resulting revision", first.text)

        second = self.run_tool("check_command", {"cmd": command})
        self.assertEqual(second.rc, 0)
        self.assertEqual(self.st.rev, 1)
        self.assertEqual(self.st.ok_rev, 1)

    def test_check_can_verify_while_an_ignored_artifact_changes(self):
        Path(self.tmpdir, ".agentignore").write_text(
            "coverage.xml\n",
            encoding="utf-8",
        )
        command = (
            f'"{sys.executable}" -c "from pathlib import Path; import time; '
            "Path('coverage.xml').write_text(str(time.time_ns()), encoding='utf-8')\""
        )

        first = self.run_tool("check_command", {"cmd": command})
        second = self.run_tool("check_command", {"cmd": command})

        self.assertEqual(first.rc, 0)
        self.assertEqual(second.rc, 0)
        self.assertEqual(first.changed_files, [])
        self.assertEqual(second.changed_files, [])
        self.assertTrue(self.st.verification_current())
        self.assertEqual(self.st.rev, 0)

    def test_configured_final_verifier_must_match_and_latest_failure_invalidates(self):
        flag = Path(self.tmpdir, "flag.txt")
        flag.write_text("ok", encoding="utf-8")
        required = (
            f'"{sys.executable}" -c "import pathlib,sys; '
            "sys.exit(pathlib.Path('flag.txt').read_text() != 'ok')\""
        )
        self.st.rev = 2
        self.st.required_verifier = required
        other = self.run_tool(
            "check_command",
            {"cmd": f'"{sys.executable}" -c "print(\'other\')"'},
        )
        self.assertEqual(other.rc, 0)
        self.assertEqual(self.st.ok_rev, -1)
        self.assertIn("did not satisfy", other.text)

        passed = self.run_tool("check_command", {"cmd": required})
        self.assertEqual(self.st.ok_rev, 2)
        self.assertIn("Verified workspace revision 2", passed.text)

        flag.write_text("bad", encoding="utf-8")
        failed = self.run_tool("check_command", {"cmd": required})
        self.assertEqual(failed.rc, 1)
        self.assertEqual(self.st.ok_rev, -1)


if __name__ == "__main__":
    unittest.main()
