"""Load one bounded project-context file without adding a memory subsystem."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class ProjectContext:
    path: Path
    text: str
    original_chars: int
    truncated: bool


def load_project_context(workspace: str | Path) -> ProjectContext | None:
    path = Path(workspace).resolve() / "AGENTS.md"
    if not path.is_file():
        return None
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        return None
    text = data.decode("utf-8", errors="replace")
    original_chars = len(text)
    truncated = original_chars > config.MAX_PROJECT_CONTEXT_CHARS
    if truncated:
        text = (
            text[: config.MAX_PROJECT_CONTEXT_CHARS]
            + f"\n\n[AGENTS.md truncated from {original_chars} characters by the runtime.]"
        )
    return ProjectContext(path=path, text=text, original_chars=original_chars, truncated=truncated)
