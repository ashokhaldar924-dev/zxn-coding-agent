"""Small, explicit data structures shared by the runtime and tools."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import config
from gitguard import GitGuard
from guards import RepetitionGuard
from permissions import PermissionManager


@dataclass
class ToolRes:
    """Result of running the local tool runtime.

    ``ok`` describes whether the runtime itself worked. A command that ran and
    returned a non-zero exit code is still an observation with ``ok=True``.
    """

    text: str
    ok: bool = True
    rc: int | None = None
    rejected: bool = False
    blocked: bool = False
    block_kind: str | None = None


@dataclass
class State:
    step: int = 0
    errs: int = 0
    rev: int = 0
    ok_rev: int = -1
    changed: bool = False
    files: set[str] = field(default_factory=set)
    in_tok: int = 0
    out_tok: int = 0
    last_check_cmd: str | None = None
    last_check_rc: int | None = None
    last_check_rev: int | None = None
    external_change_possible: bool = False
    required_verifier: str | None = None
    session_id: str | None = None
    start: float = field(default_factory=time.time)
    permissions: PermissionManager = field(default_factory=PermissionManager)
    git_guard: GitGuard = field(default_factory=GitGuard)
    repetition: RepetitionGuard = field(
        default_factory=lambda: RepetitionGuard(config.MAX_IDENTICAL_CALLS)
    )
    checkpoints: Any = None

    def begin_turn(self) -> None:
        """Reset limits that belong to one user turn, not the whole session."""

        self.step = 0
        self.errs = 0
        self.start = time.time()
        self.repetition.reset()

    def runtime_context(self) -> str:
        """Return a deterministic, compact summary for every model request."""

        verification = (
            f"verified revision {self.ok_rev}"
            if self.changed and self.ok_rev == self.rev
            else "verification required"
            if self.changed
            else "no Agent file edits require verification"
        )
        files = ", ".join(sorted(self.files)) if self.files else "none"
        check = "none"
        if self.last_check_cmd is not None:
            check = (
                f"revision {self.last_check_rev}, exit {self.last_check_rc}: "
                f"{self.last_check_cmd[:240]}"
            )
        required = self.required_verifier or "model-selected check_command"
        return (
            "[Runtime state - maintained by the local runtime, not by the model]\n"
            f"workspace revision: {self.rev}; {verification}\n"
            f"files changed through Agent edit tools: {files}\n"
            f"latest check: {check}\n"
            f"required final verifier: {required}\n"
            f"external shell change possible: {'yes' if self.external_change_possible else 'no'}"
        )

    def note_user_shell(self) -> None:
        """Conservatively invalidate verification after a user-entered shell command."""

        self.rev += 1
        self.ok_rev = -1
        self.external_change_possible = True
