"""Small session-scoped permission policy for effect tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

import config


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionResult:
    decision: Decision
    reason: str
    user_rejected: bool = False
    remembered: bool = False


_DENIED_COMMANDS = [
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git reset --hard can discard work"),
    (re.compile(r"\bgit\s+clean\b[^\r\n]*\s-[^\s]*f", re.IGNORECASE), "git clean -f can delete untracked files"),
    (re.compile(r"\b(?:shutdown|reboot|poweroff)\b", re.IGNORECASE), "machine power commands are outside the task boundary"),
    (re.compile(r"\b(?:diskpart|mkfs(?:\.[a-z0-9]+)?|format(?:\.com)?)\b", re.IGNORECASE), "disk formatting commands are denied"),
    (
        re.compile(r"\brm\s+-(?=[^\r\n]*r)(?=[^\r\n]*f)[^\r\n]*\s+(?:/|~)(?:\s|$)", re.IGNORECASE),
        "recursive deletion of a system or home root is denied",
    ),
    (
        re.compile(
            r"\bRemove-Item\b(?=[^\r\n]*-Recurse)(?=[^\r\n]*-Force)"
            r"(?=[^\r\n]*(?:[a-z]:\\(?:[\s\"']|$)|\$HOME(?:[\\/\s\"']|$)|~(?:[\\/\s\"']|$)))",
            re.IGNORECASE,
        ),
        "recursive forced deletion of a drive or home root is denied",
    ),
    (
        re.compile(
            r"\b(?:rd|rmdir|del)\b(?=[^\r\n]*/s)(?=[^\r\n]*/q)"
            r"(?=[^\r\n]*[a-z]:\\(?:[\s\"'*]|$))",
            re.IGNORECASE,
        ),
        "recursive forced deletion of a drive root is denied",
    ),
]


@dataclass
class PermissionManager:
    """Resolve effect-tool permissions and remember approvals for one run."""

    allow_clean_edits: bool = False
    allowed_commands: set[str] = field(default_factory=set)
    allowed_dirty_files: set[str] = field(default_factory=set)

    @staticmethod
    def _answer(prompt: str) -> str:
        try:
            return input(prompt).strip().lower()
        except EOFError:
            return ""

    @staticmethod
    def _denied_command_reason(cmd: str) -> str | None:
        for pattern, reason in _DENIED_COMMANDS:
            if pattern.search(cmd):
                return reason
        return None

    def decide_edit(self, path: str, *, initially_dirty: bool = False) -> PermissionResult:
        if not config.REQUIRE_CONFIRMATION:
            return PermissionResult(Decision.ALLOW, "confirmation disabled for this run")
        if initially_dirty:
            if path in self.allowed_dirty_files:
                return PermissionResult(Decision.ALLOW, "initially dirty file approved for this session")
            return PermissionResult(Decision.ASK, "initially dirty file needs specific approval")
        if self.allow_clean_edits:
            return PermissionResult(Decision.ALLOW, "clean-file edits approved for this session")
        return PermissionResult(Decision.ASK, "clean-file edit needs approval")

    def authorize_edit(self, path: str, *, initially_dirty: bool = False) -> PermissionResult:
        decision = self.decide_edit(path, initially_dirty=initially_dirty)
        if decision.decision is not Decision.ASK:
            return decision

        if initially_dirty:
            answer = self._answer(
                f"WARNING: {path} had user changes before this run. "
                "Apply this change? [y] once / [a] allow this file for session / [N]: "
            )
            if answer == "a":
                self.allowed_dirty_files.add(path)
                return PermissionResult(
                    Decision.ALLOW,
                    "user approved this initially dirty file for the session",
                    remembered=True,
                )
            if answer in {"y", "yes"}:
                return PermissionResult(Decision.ALLOW, "user approved this edit once")
            return PermissionResult(
                Decision.DENY,
                "user rejected a change to an initially dirty file",
                user_rejected=True,
            )

        answer = self._answer(
            f"Apply changes to {path}? [y] once / [a] allow clean-file edits for session / [N]: "
        )
        if answer == "a":
            self.allow_clean_edits = True
            return PermissionResult(
                Decision.ALLOW,
                "user approved clean-file edits for the session",
                remembered=True,
            )
        if answer in {"y", "yes"}:
            return PermissionResult(Decision.ALLOW, "user approved this edit once")
        return PermissionResult(
            Decision.DENY,
            "user rejected this edit",
            user_rejected=True,
        )

    def decide_command(self, cmd: str) -> PermissionResult:
        denied_reason = self._denied_command_reason(cmd)
        if denied_reason:
            return PermissionResult(Decision.DENY, denied_reason)
        if not config.REQUIRE_CONFIRMATION:
            return PermissionResult(Decision.ALLOW, "confirmation disabled for this run")
        normalized = cmd.strip()
        if normalized in self.allowed_commands:
            return PermissionResult(Decision.ALLOW, "exact command approved for this session")
        return PermissionResult(Decision.ASK, "command needs approval")

    def authorize_command(self, cmd: str) -> PermissionResult:
        decision = self.decide_command(cmd)
        if decision.decision is not Decision.ASK:
            return decision

        normalized = cmd.strip()
        answer = self._answer(
            "Execute this command? [y] once / [a] allow this exact command for session / [N]: "
        )
        if answer == "a":
            self.allowed_commands.add(normalized)
            return PermissionResult(
                Decision.ALLOW,
                "user approved this exact command for the session",
                remembered=True,
            )
        if answer in {"y", "yes"}:
            return PermissionResult(Decision.ALLOW, "user approved this command once")
        return PermissionResult(
            Decision.DENY,
            "user rejected this command",
            user_rejected=True,
        )
