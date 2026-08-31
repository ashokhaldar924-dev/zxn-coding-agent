from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zxn_agent import config


class TestPersistentEnvironmentConfig(unittest.TestCase):
    def test_process_environment_has_precedence(self):
        with patch.dict(
            os.environ, {"AGENT_TEST_SETTING": "process"}, clear=False
        ), patch.object(config, "_windows_user_environment", return_value="persistent"):
            self.assertEqual(config._setting("AGENT_TEST_SETTING"), "process")

    def test_persistent_user_environment_is_a_fallback(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(
            config, "_windows_user_environment", return_value="persistent"
        ):
            os.environ.pop("AGENT_TEST_SETTING", None)
            self.assertEqual(config._setting("AGENT_TEST_SETTING"), "persistent")

    def test_api_key_uses_persistent_user_environment(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(
            config, "_windows_user_environment", return_value="saved-key"
        ):
            os.environ.pop("AGENT_API_KEY", None)
            self.assertEqual(config.get_api_key(), "saved-key")


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
