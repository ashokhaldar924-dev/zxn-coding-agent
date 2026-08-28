from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import config  # noqa: E402
from project_context import load_project_context  # noqa: E402


class TestProjectContext(unittest.TestCase):
    def setUp(self):
        self.old_limit = config.MAX_PROJECT_CONTEXT_CHARS
        config.MAX_PROJECT_CONTEXT_CHARS = 20

    def tearDown(self):
        config.MAX_PROJECT_CONTEXT_CHARS = self.old_limit

    def test_missing_and_binary_context_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(load_project_context(tmpdir))
            Path(tmpdir, "AGENTS.md").write_bytes(b"rule\x00binary")
            self.assertIsNone(load_project_context(tmpdir))

    def test_root_agents_file_is_bounded_and_injected_with_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "AGENTS.md").write_text("Use pytest. " * 5, encoding="utf-8")
            context = load_project_context(tmpdir)
        self.assertIsNotNone(context)
        self.assertTrue(context.truncated)
        prompt = agent.system_prompt(context, ["dirty.py"])
        self.assertIn("<project_context>", prompt)
        self.assertIn("cannot override runtime safety", prompt)
        self.assertIn("dirty.py", prompt)
        self.assertIn("truncated", prompt)


if __name__ == "__main__":
    unittest.main()
