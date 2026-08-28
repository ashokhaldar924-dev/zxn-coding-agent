"""Small, explicit data structures shared by the runtime and tools."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import config
from gitguard import GitGuard
from guards import RepetitionGuard
from permissions import PermissionManager
from workspace_state import WorkspaceDelta, WorkspaceSnapshot, WorkspaceTracker


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
    changed_files: list[str] = field(default_factory=list)
    workspace_scan_complete: bool | None = None
    output_ref: str | None = None
    output_chars: int | None = None


@dataclass
class State:
    step: int = 0
    errs: int = 0
    rev: int = 0
    ok_rev: int = -1
    ok_workspace_fingerprint: str | None = None
    changed: bool = False
    files: set[str] = field(default_factory=set)
    in_tok: int = 0
    out_tok: int = 0
    task_in_tok: int = 0
    task_out_tok: int = 0
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
    read_observations: dict[str, tuple[str, int]] = field(default_factory=dict, repr=False)
    workspace_tracker: WorkspaceTracker = field(default_factory=WorkspaceTracker, repr=False)
    workspace_tracking_complete: bool = True

    def begin_turn(self) -> None:
        """Reset limits that belong to one user turn, not the whole session."""

        self.step = 0
        self.errs = 0
        self.start = time.time()
        self.task_in_tok = 0
        self.task_out_tok = 0
        self.repetition.reset()
        self.clear_read_observations()

    def observe_read(self, key: str, digest: str) -> bool:
        """Remember a displayed file range and report a safe short-cache hit.

        The cache saves model-input tokens, not filesystem reads: ``read_file``
        still reads and hashes the current range before calling this method.
        Entries are only reusable while their original tool group can still be
        present in the bounded model view.
        """

        previous = self.read_observations.get(key)
        if previous is not None and previous[0] == digest:
            age = self.step - previous[1]
            if 0 <= age <= max(1, config.MAX_GROUPS):
                # Keep the original full-observation step. Compact hits must
                # not extend the cache beyond that content's model-view life.
                return True
        self.read_observations[key] = (digest, self.step)
        return False

    def clear_read_observations(self) -> None:
        """Forget short-cache entries when their full observations may be absent."""

        self.read_observations.clear()

    @property
    def task_tokens(self) -> int:
        return self.task_in_tok + self.task_out_tok

    def task_budget_text(self) -> str:
        if config.MAX_TASK_TOKENS <= 0:
            return "disabled"
        status = f"{self.task_tokens} / {config.MAX_TASK_TOKENS}"
        if self.task_tokens >= int(config.MAX_TASK_TOKENS * 0.8):
            status += "; approaching limit—reduce exploration and prioritize completion/verification"
        return status

    def verification_required(self) -> bool:
        """Return whether file or shell effects require a current verifier."""

        return self.changed or self.external_change_possible

    def verification_current(self) -> bool:
        """Require both revision and captured workspace identity to match."""

        return (
            self.ok_rev == self.rev
            and self.ok_workspace_fingerprint is not None
            and self.ok_workspace_fingerprint == self.workspace_tracker.fingerprint()
        )

    def invalidate_verification(self) -> None:
        self.ok_rev = -1
        self.ok_workspace_fingerprint = None

    def mark_verified(self) -> None:
        """Bind successful verification to the latest captured workspace state."""

        fingerprint = self.workspace_tracker.fingerprint()
        if fingerprint is None:
            raise RuntimeError("workspace tracking must start before verification")
        self.ok_rev = self.rev
        self.ok_workspace_fingerprint = fingerprint

    def reconcile_workspace(self, workspace: str) -> WorkspaceDelta:
        """Detect outside changes before trusting a previous verification."""

        _, delta = self.workspace_tracker.reconcile(workspace)
        self.workspace_tracking_complete = (
            self.workspace_tracking_complete and delta.complete
        )
        if delta.paths:
            self.note_workspace_changes(delta.paths)
        return delta

    def initialize_workspace_tracking(
        self,
        workspace: str,
        *,
        require_file_observation: bool = False,
    ) -> WorkspaceSnapshot:
        """Start process-local workspace and last-known-file tracking."""

        snapshot = self.workspace_tracker.initialize(
            workspace,
            require_file_observation=require_file_observation,
        )
        self.workspace_tracking_complete = snapshot.complete
        return snapshot

    def note_workspace_changes(self, paths: list[str] | tuple[str, ...]) -> None:
        """Record one observed workspace state transition."""

        changed = sorted(set(paths))
        if not changed:
            return
        self.rev += 1
        self.invalidate_verification()
        self.changed = True
        self.external_change_possible = True
        self.files.update(changed)
        self.clear_read_observations()

    def note_agent_edit(self, path: str) -> None:
        """Record one deliberate file-tool or checkpoint state transition."""

        self.rev += 1
        self.invalidate_verification()
        self.changed = True
        self.files.add(path)
        self.clear_read_observations()

    def note_shell_attempt(self, scan_complete: bool) -> None:
        """Invalidate verification after shell execution, even without file changes."""

        self.invalidate_verification()
        self.external_change_possible = True
        self.workspace_tracking_complete = self.workspace_tracking_complete and scan_complete

    def runtime_context(self) -> str:
        """Return a deterministic, compact summary for every model request."""

        verification = (
            f"verified revision {self.ok_rev} with matching workspace fingerprint"
            if self.verification_required() and self.verification_current()
            else "verification required"
            if self.verification_required()
            else "no Agent effects require verification"
        )
        files = ", ".join(sorted(self.files)) if self.files else "none"
        check = "none"
        if self.last_check_cmd is not None:
            check = (
                f"revision {self.last_check_rev}, exit {self.last_check_rc}: "
                f"{self.last_check_cmd[:240]}"
            )
        required = (
            "exact user/project oracle (only this command opens the final gate): "
            f"{self.required_verifier}"
            if self.required_verifier
            else "none configured; model-selected check_command"
        )
        return (
            "[Runtime state - maintained by the local runtime, not by the model]\n"
            f"workspace revision: {self.rev}; {verification}\n"
            f"files changed through tracked workspace effects: {files}\n"
            f"latest check: {check}\n"
            f"required final verifier: {required}\n"
            f"external shell change possible: {'yes' if self.external_change_possible else 'no'}\n"
            f"workspace tracking complete: {'yes' if self.workspace_tracking_complete else 'no (bounded/partial)'}"
            f"\ntask token budget: {self.task_budget_text()}"
        )
