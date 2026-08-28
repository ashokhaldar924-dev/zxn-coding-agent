"""Small, explicit data structures shared by the runtime and tools."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from gitguard import GitGuard
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
    start: float = field(default_factory=time.time)
    permissions: PermissionManager = field(default_factory=PermissionManager)
    git_guard: GitGuard = field(default_factory=GitGuard)
