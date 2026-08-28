from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


class TestFinalVerifierConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.old = os.environ.get("AGENT_FINAL_VERIFIER")
        os.environ.pop("AGENT_FINAL_VERIFIER", None)

    def tearDown(self):
        if self.old is None:
            os.environ.pop("AGENT_FINAL_VERIFIER", None)
        else:
            os.environ["AGENT_FINAL_VERIFIER"] = self.old
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_project_verifier_and_environment_precedence(self):
        Path(self.tmpdir, ".agent-verifier").write_text("python -m unittest\n", encoding="utf-8")
        self.assertEqual(
            config.get_final_verifier(self.tmpdir),
            "python -m unittest",
        )
        os.environ["AGENT_FINAL_VERIFIER"] = "pytest -q"
        self.assertEqual(config.get_final_verifier(self.tmpdir), "pytest -q")

    def test_invalid_project_verifier_is_rejected(self):
        Path(self.tmpdir, ".agent-verifier").write_bytes(b"\x00bad")
        with self.assertRaises(RuntimeError):
            config.get_final_verifier(self.tmpdir)


if __name__ == "__main__":
    unittest.main()
