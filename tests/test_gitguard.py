from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitguard import GitGuard


class TestGitGuard(unittest.TestCase):
    def test_scan_parses_tracked_untracked_and_renamed_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir).resolve()
            root_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=str(repo) + "\n",
                stderr="",
            )
            status_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=" M tracked.py\0?? new file.py\0R  renamed.py\0old.py\0",
                stderr="",
            )
            head_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="abc123\n",
                stderr="",
            )
            with mock.patch(
                "gitguard.subprocess.run",
                side_effect=[root_result, head_result, status_result],
            ):
                guard = GitGuard.scan(repo)
            self.assertTrue(guard.active)
            self.assertEqual(guard.head, "abc123")
            self.assertEqual(
                guard.initial_dirty,
                {
                    (repo / "tracked.py").resolve(),
                    (repo / "new file.py").resolve(),
                    (repo / "renamed.py").resolve(),
                },
            )
            self.assertTrue(guard.is_initially_dirty(repo / "tracked.py"))
            self.assertEqual(
                guard.display_paths(repo),
                ["new file.py", "renamed.py", "tracked.py"],
            )

    def test_scan_degrades_cleanly_outside_git(self):
        failed = subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="not git")
        with mock.patch("gitguard.subprocess.run", return_value=failed):
            guard = GitGuard.scan("D:/not-a-repository")
        self.assertFalse(guard.active)
        self.assertEqual(guard.initial_dirty, set())
        self.assertIsNone(guard.head)

    def test_display_paths_omits_dirty_files_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            workspace = repo / "sub"
            workspace.mkdir()
            inside = workspace / "inside.py"
            outside = repo / "outside.py"
            guard = GitGuard(repo_root=repo, initial_dirty={inside.resolve(), outside.resolve()})
            self.assertEqual(guard.display_paths(workspace), ["inside.py"])


if __name__ == "__main__":
    unittest.main()
