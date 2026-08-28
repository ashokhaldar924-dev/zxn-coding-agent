from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gitguard import GitGuard  # noqa: E402


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
            with mock.patch("gitguard.subprocess.run", side_effect=[root_result, status_result]):
                guard = GitGuard.scan(repo)
            self.assertTrue(guard.active)
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


if __name__ == "__main__":
    unittest.main()
