from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from permissions import Decision, PermissionManager


class TestPermissionManager(unittest.TestCase):
    def setUp(self):
        self.old_confirm = config.REQUIRE_CONFIRMATION
        self.old_mode = config.PERMISSION_MODE
        config.REQUIRE_CONFIRMATION = True
        config.PERMISSION_MODE = "balanced"
        self.permissions = PermissionManager()

    def tearDown(self):
        config.REQUIRE_CONFIRMATION = self.old_confirm
        config.PERMISSION_MODE = self.old_mode

    def test_balanced_mode_auto_approves_ordinary_workspace_edits(self):
        with mock.patch("builtins.input") as ask:
            first = self.permissions.authorize_edit("a.py")
            second = self.permissions.authorize_edit("src/b.py")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_not_called()

    def test_manual_mode_can_remember_ordinary_edit_approval(self):
        config.PERMISSION_MODE = "manual"
        with mock.patch("builtins.input", return_value="2") as ask:
            first = self.permissions.authorize_edit("a.py")
        second = self.permissions.authorize_edit("b.py")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_unapproved_effects_have_explicit_ask_decision(self):
        self.assertEqual(
            self.permissions.decide_edit("a.py").decision,
            Decision.ALLOW,
        )
        self.assertEqual(
            self.permissions.decide_edit("dirty.py", initially_dirty=True).decision,
            Decision.ASK,
        )
        self.assertEqual(
            self.permissions.decide_command("python -m unittest").decision,
            Decision.ASK,
        )

    def test_command_family_session_approval_is_remembered(self):
        with mock.patch("builtins.input", return_value="2") as ask:
            first = self.permissions.authorize_command("mytool inspect a.py")
        second = self.permissions.authorize_command("mytool inspect b.py")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_different_command_still_asks(self):
        with mock.patch("builtins.input", side_effect=["2", "3"]) as ask:
            self.permissions.authorize_command("mytool inspect a.py")
            second = self.permissions.authorize_command("mytool build a.py")
        self.assertEqual(second.decision, Decision.DENY)
        self.assertTrue(second.user_rejected)
        self.assertEqual(ask.call_count, 2)

    def test_dirty_file_needs_specific_session_approval(self):
        self.permissions.allow_clean_edits = True
        with mock.patch("builtins.input", return_value="2") as ask:
            first = self.permissions.authorize_edit("dirty.py", initially_dirty=True)
        second = self.permissions.authorize_edit("dirty.py", initially_dirty=True)
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        ask.assert_called_once()

    def test_protected_file_needs_specific_approval(self):
        with mock.patch("builtins.input", side_effect=["2", "3"]) as ask:
            first = self.permissions.authorize_edit(".env")
            verifier = self.permissions.authorize_edit(".agent-verifier")
        second = self.permissions.authorize_edit(".env")
        ordinary_example = self.permissions.authorize_edit(".env.example")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.ALLOW)
        self.assertEqual(ordinary_example.decision, Decision.ALLOW)
        self.assertEqual(verifier.decision, Decision.DENY)
        self.assertTrue(verifier.user_rejected)
        self.assertEqual(ask.call_count, 2)

    def test_agentignore_is_protected_runtime_policy(self):
        with mock.patch("builtins.input", return_value="3"):
            result = self.permissions.authorize_edit(".agentignore")
        self.assertEqual(result.decision, Decision.DENY)
        self.assertTrue(result.user_rejected)

    def test_read_only_commands_auto_allow_but_shell_control_forces_approval(self):
        with mock.patch("builtins.input") as ask:
            status = self.permissions.authorize_command("git status --short")
            search = self.permissions.authorize_command("rg needle src")
        redirected = self.permissions.decide_command("git status > status.txt")
        outside = self.permissions.decide_command(r"type C:\Users\example\.ssh\id_rsa")
        sensitive = self.permissions.decide_command("type .env")
        environment = self.permissions.decide_command("echo %AGENT_API_KEY%")
        remote = self.permissions.decide_command("git remote -v")
        self.assertEqual(status.decision, Decision.ALLOW)
        self.assertEqual(search.decision, Decision.ALLOW)
        self.assertEqual(redirected.decision, Decision.ASK)
        self.assertEqual(outside.decision, Decision.ASK)
        self.assertEqual(sensitive.decision, Decision.ASK)
        self.assertEqual(environment.decision, Decision.ASK)
        self.assertEqual(remote.decision, Decision.ASK)
        ask.assert_not_called()

    def test_common_check_and_configured_verifier_are_auto_allowed(self):
        check = "python -m unittest discover -s tests -v"
        self.assertEqual(
            self.permissions.decide_command(check, verification=True).decision,
            Decision.ALLOW,
        )
        self.assertEqual(
            self.permissions.decide_command(check, verification=False).decision,
            Decision.ASK,
        )
        custom = "custom-verifier --project ."
        self.assertEqual(
            self.permissions.decide_command(
                custom,
                verification=True,
                required_verifier=custom,
            ).decision,
            Decision.ALLOW,
        )

    def test_high_impact_command_never_gains_allow_for_session(self):
        with mock.patch("builtins.input", side_effect=["1", "2"]) as ask:
            first = self.permissions.authorize_command("git push origin main")
            second = self.permissions.authorize_command("git push origin main")
        self.assertEqual(first.decision, Decision.ALLOW)
        self.assertEqual(second.decision, Decision.DENY)
        self.assertTrue(second.user_rejected)
        self.assertEqual(ask.call_count, 2)

    def test_high_impact_command_family_can_be_denied_for_session(self):
        with mock.patch("builtins.input", return_value="3") as ask:
            first = self.permissions.authorize_command("git push origin main")
        second = self.permissions.authorize_command("git push origin feature")
        self.assertEqual(first.decision, Decision.DENY)
        self.assertTrue(first.remembered)
        self.assertEqual(second.decision, Decision.DENY)
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
