from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zxn_agent import workspace_state
from zxn_agent.workspace_state import WorkspaceTracker


class TestWorkspaceState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_incremental_snapshot_reuses_unchanged_digests(self):
        first_path = self.root / "first.py"
        second_path = self.root / "second.py"
        first_path.write_text("value = 1\n", encoding="utf-8")
        second_path.write_text("value = 2\n", encoding="utf-8")
        tracker = WorkspaceTracker()

        with mock.patch(
            "zxn_agent.workspace_state._read_digest",
            wraps=workspace_state._read_digest,
        ) as read_digest:
            initial = tracker.initialize(self.root)
            unchanged, unchanged_delta = tracker.reconcile(self.root)
            first_path.write_text("value = 3\n", encoding="utf-8")
            changed, changed_delta = tracker.reconcile(self.root)

        self.assertEqual(initial.hashed_files, 2)
        self.assertEqual(unchanged.hashed_files, 0)
        self.assertEqual(unchanged.reused_files, 2)
        self.assertEqual(unchanged_delta.paths, ())
        self.assertEqual(changed.hashed_files, 1)
        self.assertEqual(changed.reused_files, 1)
        self.assertEqual(changed_delta.paths, ("first.py",))
        self.assertEqual(read_digest.call_count, 3)

    def test_agentignore_excludes_only_explicit_regenerable_outputs(self):
        (self.root / ".agentignore").write_text(
            "coverage.xml\nbuild/\n*.log\n!keep.log\n/root-only.txt\n\\!literal.txt\n",
            encoding="utf-8",
        )
        (self.root / "source.py").write_text("before\n", encoding="utf-8")
        (self.root / "coverage.xml").write_text("one\n", encoding="utf-8")
        (self.root / "run.log").write_text("one\n", encoding="utf-8")
        (self.root / "keep.log").write_text("kept\n", encoding="utf-8")
        (self.root / "root-only.txt").write_text("ignored\n", encoding="utf-8")
        (self.root / "!literal.txt").write_text("ignored\n", encoding="utf-8")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "root-only.txt").write_text("visible\n", encoding="utf-8")
        (self.root / "build").mkdir()
        (self.root / "build" / "artifact.bin").write_bytes(b"one")
        tracker = WorkspaceTracker()

        initial = tracker.initialize(self.root)
        self.assertIn(".agentignore", initial.files)
        self.assertIn("source.py", initial.files)
        self.assertIn("keep.log", initial.files)
        self.assertIn("nested/root-only.txt", initial.files)
        self.assertNotIn("root-only.txt", initial.files)
        self.assertNotIn("!literal.txt", initial.files)
        self.assertNotIn("coverage.xml", initial.files)
        self.assertNotIn("run.log", initial.files)
        self.assertNotIn("build/artifact.bin", initial.files)

        (self.root / "coverage.xml").write_text("two\n", encoding="utf-8")
        (self.root / "build" / "artifact.bin").write_bytes(b"two")
        _, artifact_delta = tracker.reconcile(self.root)
        self.assertEqual(artifact_delta.paths, ())

        (self.root / "source.py").write_text("after\n", encoding="utf-8")
        _, source_delta = tracker.reconcile(self.root)
        self.assertEqual(source_delta.paths, ("source.py",))

    def test_gitignore_never_hides_a_tracked_file(self):
        (self.root / ".gitignore").write_text("*.py\n*.log\n", encoding="utf-8")
        (self.root / "tracked.py").write_text("tracked\n", encoding="utf-8")
        (self.root / "untracked.py").write_text("untracked\n", encoding="utf-8")
        (self.root / "output.log").write_text("noise\n", encoding="utf-8")

        with mock.patch(
            "zxn_agent.workspace_state._git_tracked_paths",
            return_value=frozenset({".gitignore", "tracked.py"}),
        ):
            snapshot = WorkspaceTracker().initialize(self.root)

        self.assertIn(".gitignore", snapshot.files)
        self.assertIn("tracked.py", snapshot.files)
        self.assertNotIn("untracked.py", snapshot.files)
        self.assertNotIn("output.log", snapshot.files)

    def test_ignore_policy_is_pinned_for_the_process(self):
        ignore_path = self.root / ".agentignore"
        ignore_path.write_text("coverage.xml\n", encoding="utf-8")
        tracker = WorkspaceTracker()
        tracker.initialize(self.root)

        ignore_path.write_text("coverage.xml\nnew.py\n", encoding="utf-8")
        (self.root / "new.py").write_text("must remain visible\n", encoding="utf-8")
        current, delta = tracker.reconcile(self.root)

        self.assertIn("new.py", current.files)
        self.assertEqual(delta.paths, (".agentignore", "new.py"))


if __name__ == "__main__":
    unittest.main()
