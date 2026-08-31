from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from zxn_agent import config
from zxn_agent.interactive import expand_file_references, shell_observation, status_text
from zxn_agent.state import State, ToolRes


class TestInteractiveHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old_limit = config.MAX_FILE_REFERENCE_CHARS
        self.old_total_limit = config.MAX_FILE_REFERENCE_TOTAL_CHARS
        config.MAX_FILE_REFERENCE_CHARS = 8
        config.MAX_FILE_REFERENCE_TOTAL_CHARS = 12

    def tearDown(self):
        config.MAX_FILE_REFERENCE_CHARS = self.old_limit
        config.MAX_FILE_REFERENCE_TOTAL_CHARS = self.old_total_limit
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_file_reference_is_bounded_and_workspace_safe(self):
        Path(self.tmpdir, "a.txt").write_text("0123456789", encoding="utf-8")
        expanded, refs = expand_file_references("inspect @a.txt", self.tmpdir)
        self.assertEqual(refs, ["a.txt"])
        self.assertIn("01234567", expanded)
        self.assertIn("truncated from 10", expanded)

        expanded, _ = expand_file_references("inspect @../secret.txt", self.tmpdir)
        self.assertIn("escapes the workspace", expanded)

    def test_file_references_share_one_aggregate_budget(self):
        Path(self.tmpdir, "a.txt").write_text("abcdefgh", encoding="utf-8")
        Path(self.tmpdir, "b.txt").write_text("ijklmnop", encoding="utf-8")

        expanded, refs = expand_file_references("inspect @a.txt @b.txt", self.tmpdir)

        self.assertEqual(refs, ["a.txt", "b.txt"])
        self.assertIn('<referenced_file path="a.txt">\nabcdefgh', expanded)
        self.assertIn('<referenced_file path="b.txt">\nijkl', expanded)
        self.assertNotIn("ijklmnop\n</referenced_file>", expanded)
        self.assertIn("truncated from 8", expanded)

    def test_shell_observation_and_status_are_explicit(self):
        observation = shell_observation("pytest -q", ToolRes("exit code: 0", rc=0))
        self.assertIn("user explicitly ran", observation)
        self.assertIn("exit code: 0", observation)
        st = State(rev=1, changed=True, ok_rev=-1, files={"a.py"}, session_id="abc")
        status = status_text(st, Path("session.jsonl"), 2)
        self.assertIn(f"permission mode: {config.PERMISSION_MODE}", status)
        self.assertIn("verified current revision: no", status)
        self.assertIn("active checkpoints: 2", status)
        self.assertIn("current task 0", status)


if __name__ == "__main__":
    unittest.main()
