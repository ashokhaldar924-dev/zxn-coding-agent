"""Small, explicit data structures shared by the runtime and tools."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Event
from typing import Any

from . import config
from .changes import FileChange
from .gitguard import GitGuard
from .guards import RepetitionGuard
from .permissions import PermissionManager
from .planner import PlanState
from .verification import (
    FULL_SUITE,
    PROGRESS_FAILED,
    PROGRESS_NO_PROGRESS,
    PROGRESS_NOT_CHECKED,
    PROGRESS_PASSED,
    PROGRESS_WARNING,
    failure_fingerprint,
)
from .workspace_state import WorkspaceDelta, WorkspaceSnapshot, WorkspaceTracker

MAX_RECENT_FILE_EVIDENCE_CHARS = min(
    34_000,
    max(4_000, config.MAX_CONTEXT_CHARS * 9 // 16),
)
MAX_RECENT_FILE_EVIDENCE_RANGES = 12


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
    elapsed_seconds: float | None = None
    file_changes: list[FileChange] = field(default_factory=list)
    plan_updated: bool = False
    cancelled: bool = False


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
    task_cache_hit_tok: int = 0
    task_cache_miss_tok: int = 0
    task_reasoning_tok: int = 0
    task_cache_usage_reported: bool = False
    task_reasoning_usage_reported: bool = False
    task_model_calls: int = 0
    task_tool_calls: int = 0
    check_attempts: list[dict[str, Any]] = field(default_factory=list)
    task_evidence: list[dict[str, Any]] = field(default_factory=list)
    repair_progress: str = PROGRESS_NOT_CHECKED
    last_check_cmd: str | None = None
    last_check_rc: int | None = None
    last_check_rev: int | None = None
    external_change_possible: bool = False
    required_verifier: str | None = None
    requires_full_verification: bool = False
    verified_scope: str | None = None
    session_id: str | None = None
    plan: PlanState = field(default_factory=PlanState)
    start: float = field(default_factory=time.time)
    permissions: PermissionManager = field(default_factory=PermissionManager)
    git_guard: GitGuard = field(default_factory=GitGuard)
    repetition: RepetitionGuard = field(
        default_factory=lambda: RepetitionGuard(config.MAX_IDENTICAL_CALLS)
    )
    checkpoints: Any = None
    read_observations: dict[str, tuple[str, int]] = field(default_factory=dict, repr=False)
    inspected_ranges: dict[str, int] = field(default_factory=dict, repr=False)
    recent_file_evidence: dict[str, str] = field(default_factory=dict, repr=False)
    workspace_tracker: WorkspaceTracker = field(default_factory=WorkspaceTracker, repr=False)
    workspace_tracking_complete: bool = True
    completed: bool = False
    termination_reason: str | None = None
    turn_checkpoint_index: int = field(default=0, repr=False)
    turn_files: set[str] = field(default_factory=set, repr=False)
    planner_task: str = field(default="", repr=False)
    planner_existing_workspace: bool = field(default=False, repr=False)
    planner_observations: set[str] = field(default_factory=set, repr=False)
    planner_plan_created_this_turn: bool = field(default=False, repr=False)
    cancel_event: Event | None = field(default=None, repr=False)
    last_failure_fingerprint: str | None = field(default=None, repr=False)
    repeated_failure_streak: int = field(default=0, repr=False)
    no_progress: bool = field(default=False, repr=False)
    length_continuations: int = field(default=0, repr=False)

    def begin_turn(
        self,
        *,
        requires_full_verification: bool | None = None,
        task: str | None = None,
    ) -> None:
        """Reset limits that belong to one user turn, not the whole session."""

        self.step = 0
        self.errs = 0
        self.start = time.time()
        self.task_in_tok = 0
        self.task_out_tok = 0
        self.task_cache_hit_tok = 0
        self.task_cache_miss_tok = 0
        self.task_reasoning_tok = 0
        self.task_cache_usage_reported = False
        self.task_reasoning_usage_reported = False
        self.task_model_calls = 0
        self.task_tool_calls = 0
        self.check_attempts.clear()
        self.task_evidence.clear()
        self.repair_progress = PROGRESS_NOT_CHECKED
        self.last_failure_fingerprint = None
        self.repeated_failure_streak = 0
        self.no_progress = False
        self.length_continuations = 0
        self.repetition.reset()
        self.clear_read_observations()
        self.inspected_ranges.clear()
        self.recent_file_evidence.clear()
        self.completed = False
        self.termination_reason = None
        self.turn_files.clear()
        self.planner_task = task or ""
        snapshot = self.workspace_tracker.last_snapshot
        self.planner_existing_workspace = bool(snapshot and snapshot.files)
        self.planner_observations.clear()
        self.planner_plan_created_this_turn = False
        if self.cancel_event is not None:
            self.cancel_event.clear()
        if requires_full_verification is not None:
            self.requires_full_verification = requires_full_verification
        self.turn_checkpoint_index = (
            len(self.checkpoints.active()) if self.checkpoints is not None else 0
        )

    def note_planner_observation(self, tool_name: str, args: dict | None = None) -> None:
        """Record process-local evidence used before the first plan this turn."""

        self.planner_observations.add(tool_name)

    def request_cancel(self) -> None:
        """Request cooperative cancellation from a UI or embedding host."""

        if self.cancel_event is not None:
            self.cancel_event.set()

    def stop_requested(self) -> bool:
        return bool(self.cancel_event is not None and self.cancel_event.is_set())

    def note_model_call(self) -> None:
        self.task_model_calls += 1

    def note_tool_call(self) -> None:
        self.task_tool_calls += 1

    def note_evidence(self, fact: dict[str, Any]) -> None:
        """Keep bounded Runtime facts for the current task's evidence report."""

        clean = {str(key): value for key, value in fact.items() if value is not None}
        clean.setdefault("step", self.step)
        self.task_evidence.append(clean)
        if len(self.task_evidence) > 100:
            del self.task_evidence[:-100]

    def note_check_attempt(self, cmd: str, text: str, rc: int, scope: str) -> str:
        """Track repair progress without changing verification-gate semantics."""

        fingerprint = None
        streak = 0
        if rc == 0:
            self.repair_progress = PROGRESS_PASSED
            self.last_failure_fingerprint = None
            self.repeated_failure_streak = 0
            self.no_progress = False
        else:
            fingerprint, _ = failure_fingerprint(text)
            if fingerprint == self.last_failure_fingerprint:
                self.repeated_failure_streak += 1
            else:
                self.last_failure_fingerprint = fingerprint
                self.repeated_failure_streak = 1
            streak = self.repeated_failure_streak
            if streak >= 3:
                self.repair_progress = PROGRESS_NO_PROGRESS
                self.no_progress = True
            elif streak == 2:
                self.repair_progress = PROGRESS_WARNING
            else:
                self.repair_progress = PROGRESS_FAILED
        attempt = {
            "step": self.step,
            "command": cmd[:500],
            "rc": rc,
            "scope": scope,
            "progress": self.repair_progress,
            "failure_fingerprint": fingerprint,
            "same_failure_streak": streak,
        }
        self.check_attempts.append(attempt)
        if len(self.check_attempts) > 20:
            del self.check_attempts[:-20]
        self.note_evidence({
            "kind": "verification",
            "command": cmd[:500],
            "rc": rc,
            "scope": scope,
            "progress": self.repair_progress,
            "failure_fingerprint": fingerprint,
        })
        return self.repair_progress

    def observe_read(self, key: str, digest: str, payload: str) -> bool:
        """Remember a displayed file range and report a safe short-cache hit.

        The cache saves model-input tokens, not filesystem reads: ``read_file``
        still reads and hashes the current range before calling this method.
        Exact recent evidence is retained in a separate bounded working set, so
        a compact hit remains valid after its original tool group is pruned.
        """

        self.inspected_ranges.pop(key, None)
        self.inspected_ranges[key] = self.step
        if len(self.inspected_ranges) > 24:
            oldest = next(iter(self.inspected_ranges))
            del self.inspected_ranges[oldest]
            self.read_observations.pop(oldest, None)

        already_retained = self._has_file_evidence(key)
        previous = self.read_observations.get(key)
        if (
            previous is not None
            and previous[0] == digest
            and already_retained
        ):
            self._remember_file_evidence(key, payload)
            return True
        self._remember_file_evidence(key, payload)
        self.read_observations[key] = (digest, self.step)
        return False

    def clear_read_observations(self) -> None:
        """Forget short-cache entries when their full observations may be absent."""

        self.read_observations.clear()

    def clear_file_evidence(self) -> None:
        """Conservatively forget all file evidence after an incomplete scan."""

        self.clear_read_observations()
        self.inspected_ranges.clear()
        self.recent_file_evidence.clear()

    def inspected_ranges_text(self, *, limit: int = 12) -> str:
        """Return a compact durable ledger without replaying source contents."""

        if not self.inspected_ranges:
            return "none"
        keys = list(self.inspected_ranges)[-max(1, limit) :]
        prefix = "…; " if len(self.inspected_ranges) > len(keys) else ""
        return prefix + "; ".join(keys)

    def retained_file_evidence_payloads(self) -> tuple[str, ...]:
        """Identify exact read results whose original groups should stay visible."""

        return tuple(self.recent_file_evidence.values())

    def retain_visible_file_evidence(self, payloads: frozenset[str]) -> None:
        """Drop evidence whose exact tool result was absent from the model request."""

        for key, payload in list(self.recent_file_evidence.items()):
            if payload not in payloads:
                self.recent_file_evidence.pop(key, None)
                self.read_observations.pop(key, None)

    def _remember_file_evidence(self, key: str, payload: str) -> None:
        """Keep a bounded LRU of non-duplicated file ranges for future requests."""

        parsed = _read_range(key)
        if parsed is None or not payload:
            return
        path, start, end = parsed
        for existing_key in list(self.recent_file_evidence):
            existing = _read_range(existing_key)
            if existing is None or existing[0] != path:
                continue
            _, existing_start, existing_end = existing
            if existing_start <= start and existing_end >= end:
                value = self.recent_file_evidence.pop(existing_key)
                self.recent_file_evidence[existing_key] = value
                return
            if start <= existing_start and end >= existing_end:
                self.recent_file_evidence.pop(existing_key, None)

        self.recent_file_evidence.pop(key, None)
        self.recent_file_evidence[key] = payload
        while (
            len(self.recent_file_evidence) > MAX_RECENT_FILE_EVIDENCE_RANGES
            or sum(len(value) for value in self.recent_file_evidence.values())
            > MAX_RECENT_FILE_EVIDENCE_CHARS
        ):
            oldest = next(iter(self.recent_file_evidence))
            self.recent_file_evidence.pop(oldest, None)

    def _has_file_evidence(self, key: str) -> bool:
        requested = _read_range(key)
        if requested is None:
            return False
        path, start, end = requested
        return any(
            candidate is not None
            and candidate[0] == path
            and candidate[1] <= start
            and candidate[2] >= end
            for candidate in map(_read_range, self.recent_file_evidence)
        )

    def forget_inspected_paths(self, paths: list[str] | tuple[str, ...]) -> None:
        """Invalidate ledger entries whose source files changed."""

        prefixes = tuple(f"{path}:" for path in paths)
        if not prefixes:
            return
        self.inspected_ranges = {
            key: step
            for key, step in self.inspected_ranges.items()
            if not key.startswith(prefixes)
        }
        for key in list(self.recent_file_evidence):
            if key.startswith(prefixes):
                self.recent_file_evidence.pop(key, None)
                self.read_observations.pop(key, None)

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

        return (
            self.changed
            or self.external_change_possible
            or self.requires_full_verification
        )

    def verification_current(self) -> bool:
        """Require both revision and captured workspace identity to match."""

        return (
            self.ok_rev == self.rev
            and self.ok_workspace_fingerprint is not None
            and self.ok_workspace_fingerprint == self.workspace_tracker.fingerprint()
        )

    def verification_data(self) -> dict[str, Any]:
        """Return the Runtime-owned verification view consumed by UIs."""

        current = self.verification_current()
        adequate = self.verification_adequate()
        return {
            "workspace_revision": self.rev,
            "verified_revision": self.ok_rev,
            "required": self.verification_required(),
            "current": current,
            "adequate": adequate,
            "satisfied": self.verification_satisfied(),
            "fingerprint_matched": current,
            "verifier": self.last_check_cmd,
            "last_check_rc": self.last_check_rc,
            "tracking_complete": self.workspace_tracking_complete,
            "required_scope": FULL_SUITE if self.requires_full_verification else "any",
            "verified_scope": self.verified_scope,
            "progress": self.repair_progress,
            "check_attempts": len(self.check_attempts),
            "task_completed": self.completed,
        }

    def verification_adequate(self) -> bool:
        """Return whether current evidence meets this turn's explicit scope."""

        if not self.verification_current():
            return False
        if self.required_verifier:
            return True
        if self.requires_full_verification:
            return self.verified_scope == FULL_SUITE
        return True

    def verification_satisfied(self) -> bool:
        return not self.verification_required() or self.verification_adequate()

    def invalidate_verification(self) -> None:
        self.ok_rev = -1
        self.ok_workspace_fingerprint = None

    def mark_verified(self, scope: str | None = None) -> None:
        """Bind successful verification to the latest captured workspace state."""

        fingerprint = self.workspace_tracker.fingerprint()
        if fingerprint is None:
            raise RuntimeError("workspace tracking must start before verification")
        self.ok_rev = self.rev
        self.ok_workspace_fingerprint = fingerprint
        self.verified_scope = scope

    def reconcile_workspace(self, workspace: str) -> WorkspaceDelta:
        """Detect outside changes before trusting a previous verification."""

        _, delta = self.workspace_tracker.reconcile(workspace)
        self.workspace_tracking_complete = (
            self.workspace_tracking_complete and delta.complete
        )
        if not delta.complete:
            self.clear_file_evidence()
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
        self.turn_files.update(changed)
        self.forget_inspected_paths(changed)

    def note_agent_edit(self, path: str) -> None:
        """Record one deliberate file-tool or checkpoint state transition."""

        self.rev += 1
        self.invalidate_verification()
        self.changed = True
        self.files.add(path)
        self.turn_files.add(path)
        self.forget_inspected_paths([path])

    def note_shell_attempt(self, scan_complete: bool) -> None:
        """Invalidate verification after shell execution, even without file changes."""

        self.invalidate_verification()
        self.external_change_possible = True
        self.workspace_tracking_complete = self.workspace_tracking_complete and scan_complete
        if not scan_complete:
            self.clear_file_evidence()

    def runtime_context(self) -> str:
        """Return a deterministic, compact summary for every model request."""

        if self.verification_required() and self.verification_adequate():
            verification = (
                f"verified revision {self.ok_rev} with matching workspace fingerprint "
                "and adequate scope"
            )
        elif self.verification_required() and self.verification_current():
            verification = (
                f"verified revision {self.ok_rev} is current but only "
                f"{self.verified_scope or 'unknown'} scope; full-suite verification required"
            )
        elif self.verification_required():
            verification = "verification required"
        else:
            verification = "no Agent effects require verification"
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
        scope_requirement = (
            "full test suite required by the current user task"
            if self.requires_full_verification
            else "no explicit full-suite requirement"
        )
        return (
            "[Runtime state - maintained by the local runtime, not by the model]\n"
            f"workspace revision: {self.rev}; {verification}\n"
            f"files changed through tracked workspace effects: {files}\n"
            f"latest check: {check}\n"
            f"required final verifier: {required}\n"
            f"verification scope: {scope_requirement}; latest successful scope: "
            f"{self.verified_scope or 'none'}\n"
            f"repair progress: {self.repair_progress}; repeated identical failure streak: "
            f"{self.repeated_failure_streak}\n"
            f"external shell change possible: {'yes' if self.external_change_possible else 'no'}\n"
            f"workspace tracking complete: {'yes' if self.workspace_tracking_complete else 'no (bounded/partial)'}"
            f"\ntask token budget: {self.task_budget_text()}"
            f"\n{self.plan.compact()}"
            f"\ninspected file ranges: {self.inspected_ranges_text()}"
            "\nexact recent file evidence: supplied separately as bounded read_file "
            "tool results when available"
        )


def _read_range(key: str) -> tuple[str, int, int] | None:
    try:
        path, start_text, end_text = key.rsplit(":", 2)
        return path, int(start_text), int(end_text)
    except (TypeError, ValueError):
        return None
