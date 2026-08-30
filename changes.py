"""Truthful line-change summaries derived from real before/after content."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class FileChange:
    path: str
    kind: str
    additions: int | None
    deletions: int | None

    def to_data(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "additions": self.additions,
            "deletions": self.deletions,
        }

    @classmethod
    def from_data(cls, value: object) -> FileChange:
        if not isinstance(value, dict):
            raise TypeError("file change must be an object")
        path = value.get("path")
        kind = value.get("kind")
        additions = value.get("additions")
        deletions = value.get("deletions")
        if not isinstance(path, str) or not path:
            raise ValueError("file change path must be a non-empty string")
        if kind not in {"added", "modified", "deleted", "changed"}:
            raise ValueError("invalid file change kind")
        for field_name, count in (("additions", additions), ("deletions", deletions)):
            if count is not None and (
                isinstance(count, bool) or not isinstance(count, int) or count < 0
            ):
                raise ValueError(f"file change {field_name} must be a non-negative integer")
        return cls(path, str(kind), additions, deletions)


@dataclass(frozen=True)
class FileDiff:
    path: str
    kind: str
    text: str
    truncated: bool = False


def summarize_text_change(
    path: str,
    before: str | None,
    after: str | None,
) -> FileChange:
    """Count changed logical lines using the same before/after truth as a diff."""

    old_lines = [] if before is None else before.splitlines()
    new_lines = [] if after is None else after.splitlines()
    additions = 0
    deletions = 0
    for tag, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag in {"replace", "delete"}:
            deletions += old_end - old_start
        if tag in {"replace", "insert"}:
            additions += new_end - new_start
    kind = "added" if before is None else "deleted" if after is None else "modified"
    return FileChange(path, kind, additions, deletions)


def summarize_byte_change(
    path: str,
    before: bytes | None,
    after: bytes | None,
) -> FileChange:
    """Return exact text counts, or an explicit count-unavailable summary for binary data."""

    try:
        old_text = _decode(before)
        new_text = _decode(after)
    except (UnicodeDecodeError, ValueError):
        kind = "added" if before is None else "deleted" if after is None else "modified"
        return FileChange(path, kind, None, None)
    return summarize_text_change(path, old_text, new_text)


def unified_byte_diff(
    path: str,
    before: bytes | None,
    after: bytes | None,
    *,
    max_chars: int = 60_000,
) -> FileDiff:
    """Build a bounded diff from real bytes, never model-authored content."""

    if max_chars < 200:
        raise ValueError("max_chars must be >= 200")
    summary = summarize_byte_change(path, before, after)
    try:
        old_text = _decode(before) or ""
        new_text = _decode(after) or ""
    except (UnicodeDecodeError, ValueError):
        return FileDiff(path, summary.kind, "Binary diff is not available.")
    old_name = "/dev/null" if before is None else f"a/{path}"
    new_name = "/dev/null" if after is None else f"b/{path}"
    text = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=old_name,
            tofile=new_name,
        )
    )
    if not text:
        text = "No textual difference."
    if len(text) <= max_chars:
        return FileDiff(path, summary.kind, text)
    marker = "\n... diff truncated by desktop preview limit ...\n"
    room = max_chars - len(marker)
    head = room // 2
    tail = room - head
    return FileDiff(path, summary.kind, text[:head] + marker + text[-tail:], True)


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    if b"\x00" in data[:4096]:
        raise ValueError("binary data")
    payload = data.removeprefix(UTF8_BOM)
    return payload.decode("utf-8")
