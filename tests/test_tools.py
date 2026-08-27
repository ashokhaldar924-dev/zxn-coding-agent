"""
纯本地单元测试，不需要 DEEPSEEK_API_KEY，不需要网络。
只测试 tools.py 里的文件/命令工具本身是否正确、路径越权是否被正确拦截。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import tools  # noqa: E402


class TestTools(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._old_workspace = config.WORKSPACE_DIR
        self._old_confirm = config.REQUIRE_CONFIRMATION
        config.WORKSPACE_DIR = os.path.abspath(self.tmpdir)
        # 测试跑在无人值守环境下，不能卡在 input() 上等用户确认
        config.REQUIRE_CONFIRMATION = False

    def tearDown(self):
        config.WORKSPACE_DIR = self._old_workspace
        config.REQUIRE_CONFIRMATION = self._old_confirm
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_then_read_file(self):
        result = tools.tool_write_file({"path": "hello.txt", "content": "world"})
        self.assertIn("成功", result)
        content = tools.tool_read_file({"path": "hello.txt"})
        self.assertEqual(content, "world")

    def test_read_nonexistent_file_returns_error_string_not_exception(self):
        result = tools.tool_read_file({"path": "nope.txt"})
        self.assertIn("错误", result)

    def test_append_mode(self):
        tools.tool_write_file({"path": "a.txt", "content": "1"})
        tools.tool_write_file({"path": "a.txt", "content": "2", "append": True})
        self.assertEqual(tools.tool_read_file({"path": "a.txt"}), "12")

    def test_write_creates_parent_dirs(self):
        tools.tool_write_file({"path": "sub/dir/file.txt", "content": "x"})
        self.assertEqual(tools.tool_read_file({"path": "sub/dir/file.txt"}), "x")

    def test_path_traversal_is_blocked(self):
        with self.assertRaises(PermissionError):
            tools.tool_read_file({"path": "../../../etc/passwd"})

    def test_list_dir(self):
        tools.tool_write_file({"path": "f1.txt", "content": "a"})
        os.makedirs(os.path.join(config.WORKSPACE_DIR, "subdir"))
        result = tools.tool_list_dir({"path": "."})
        self.assertIn("f1.txt", result)
        self.assertIn("[DIR] subdir", result)

    def test_search_text_finds_match(self):
        tools.tool_write_file({"path": "code.py", "content": "def foo():\n    return 1\n"})
        result = tools.tool_search_text({"pattern": r"def \w+"})
        self.assertIn("code.py:1", result)

    def test_run_command_success(self):
        result = tools.tool_run_command({"command": "echo hi"})
        self.assertIn("退出码: 0", result)
        self.assertIn("hi", result)

    def test_run_command_nonzero_exit_reported_not_raised(self):
        result = tools.tool_run_command({"command": "exit 7"})
        self.assertIn("退出码: 7", result)

    def test_confirmation_rejection_blocks_write(self):
        config.REQUIRE_CONFIRMATION = True
        try:
            import builtins
            original_input = builtins.input
            builtins.input = lambda prompt="": "n"  # 模拟用户拒绝
            try:
                result = tools.tool_write_file({"path": "blocked.txt", "content": "x"})
            finally:
                builtins.input = original_input
        finally:
            config.REQUIRE_CONFIRMATION = False

        self.assertIn("拒绝", result)
        self.assertFalse(os.path.exists(os.path.join(config.WORKSPACE_DIR, "blocked.txt")))

    def test_confirmation_rejection_blocks_command(self):
        config.REQUIRE_CONFIRMATION = True
        try:
            import builtins
            original_input = builtins.input
            builtins.input = lambda prompt="": "n"
            try:
                result = tools.tool_run_command({"command": "echo should-not-run"})
            finally:
                builtins.input = original_input
        finally:
            config.REQUIRE_CONFIRMATION = False

        self.assertIn("拒绝", result)

    def test_tool_output_truncation(self):
        old_limit = config.MAX_TOOL_OUTPUT_CHARS
        config.MAX_TOOL_OUTPUT_CHARS = 10
        try:
            tools.tool_write_file({"path": "big.txt", "content": "x" * 1000})
            result = tools.tool_read_file({"path": "big.txt"})
            self.assertLess(len(result), 200)
            self.assertIn("截断", result)
        finally:
            config.MAX_TOOL_OUTPUT_CHARS = old_limit


if __name__ == "__main__":
    unittest.main()
