from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from permissions import Decision, PermissionManager  # noqa: E402


class TestPermissionManager(unittest.TestCase):
    def setUp(self):
        self.old_confirm = config.REQUIRE_CONFIRMATION
        config.REQUIRE_CONFIRMATION = True
        self.permissions = PermissionManager()

    def tearDown(self):
        config.REQUIRE_CONFIRMATION = self.old_confirm

    def test_clean_edit_session_approval_is_remembered(self):
        with mock.patch("builtins.input", return_value="a") as ask:
            first = self.permissions.authorize_edit("a.py")
        second = self.permissions.authorize_edit("b.py")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_unapproved_effects_have_explicit_ask_decision(self):
        self.assertEqual(
            self.permissions.decide_edit("a.py").decision,
            Decision.ASK,
        )
        self.assertEqual(
            self.permissions.decide_edit("dirty.py", initially_dirty=True).decision,
            Decision.ASK,
        )
        self.assertEqual(
            self.permissions.decide_command("python -m unittest").decision,
            Decision.ASK,
        )

    def test_exact_command_session_approval_is_remembered(self):
        with mock.patch("builtins.input", return_value="a") as ask:
            first = self.permissions.authorize_command("python -m unittest")
        second = self.permissions.authorize_command("python -m unittest")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_different_command_still_asks(self):
        with mock.patch("builtins.input", side_effect=["a", "n"]) as ask:
            self.permissions.authorize_command("python -m unittest")
            second = self.permissions.authorize_command("python -m py_compile a.py")
        self.assertEqual(second.decision, Decision.DENY)
        self.assertTrue(second.user_rejected)
        self.assertEqual(ask.call_count, 2)

    def test_dirty_file_needs_specific_session_approval(self):
        self.permissions.allow_clean_edits = True
        with mock.patch("builtins.input", return_value="a") as ask:
            first = self.permissions.authorize_edit("dirty.py", initially_dirty=True)
        second = self.permissions.authorize_edit("dirty.py", initially_dirty=True)
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_high_confidence_destructive_command_is_denied_without_prompt(self):
        with mock.patch("builtins.input") as ask:
            result = self.permissions.authorize_command("git reset --hard HEAD~1")
            windows = self.permissions.authorize_command(
                "Remove-Item -LiteralPath C:\\ -Recurse -Force"
            )
        self.assertEqual(result.decision, Decision.DENY)
        self.assertEqual(windows.decision, Decision.DENY)
        self.assertFalse(result.user_rejected)
        ask.assert_not_called()

    def test_confirmation_flag_allows_effects_but_not_policy_denials(self):
        config.REQUIRE_CONFIRMATION = False
        self.assertEqual(
            self.permissions.authorize_edit("a.py").decision,
            Decision.ALLOW,
        )
        self.assertEqual(
            self.permissions.authorize_command("python -m unittest").decision,
            Decision.ALLOW,
        )
        self.assertEqual(
            self.permissions.authorize_command("git reset --hard").decision,
            Decision.DENY,
        )


if __name__ == "__main__":
    unittest.main()
