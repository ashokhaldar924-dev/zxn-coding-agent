"""Deterministic guards that stay outside the agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class RepetitionGuard:
    """Block an identical consecutive tool call after a small fixed limit."""

    limit: int
    last_fingerprint: str | None = None
    count: int = 0

    def reset(self) -> None:
        """Forget only per-turn repetition state."""

        self.last_fingerprint = None
        self.count = 0

    def check(self, name: str, args: dict) -> str | None:
        fingerprint = name + ":" + json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if fingerprint == self.last_fingerprint:
            self.count += 1
        else:
            self.last_fingerprint = fingerprint
            self.count = 1
        if self.count < self.limit:
            return None
        return (
            f"Stagnation guard blocked identical call {name!r} after "
            f"{self.count} consecutive attempts. Inspect existing observations "
            "and choose a different action or arguments."
        )
