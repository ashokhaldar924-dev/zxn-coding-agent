"""Append-only JSONL trajectory, separate from the bounded model context."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import config


def redact(value: Any, secret: str) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key.lower() in {"api_key", "authorization"}:
                clean[key] = "[REDACTED]"
            else:
                clean[key] = redact(item, secret)
        return clean
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, tuple):
        return [redact(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[REDACTED]")
    return value


class RunLog:
    def __init__(self, directory: str | Path | None = None):
        folder = Path(directory) if directory is not None else Path(config.WORKSPACE_DIR) / ".agent"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        candidate = folder / f"run-{stamp}.jsonl"
        number = 2
        while candidate.exists():
            candidate = folder / f"run-{stamp}-{number}.jsonl"
            number += 1
        self.path = candidate
        self.path.touch()
        self.secret = os.environ.get("AGENT_API_KEY", "")

    def event(self, kind: str, **data: Any) -> None:
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": kind,
            **data,
        }
        safe = redact(entry, self.secret)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")


class NullLog:
    """Test-friendly logger that records events without touching the filesystem."""

    def __init__(self):
        self.events: list[dict] = []

    def event(self, kind: str, **data: Any) -> None:
        self.events.append({"event": kind, **data})
