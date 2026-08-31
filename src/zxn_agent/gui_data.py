"""Small read-only desktop data adapters outside the Qt view layer."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

NOISE_DIRS = frozenset(
    {".git", ".agent", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules"}
)
MAX_PROJECT_FILES = 800
MAX_FILE_PREVIEW_BYTES = 512_000


class WorkspaceDataError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectEntry:
    """One safe, visible file-system entry in the GUI project tree."""

    path: str
    is_directory: bool


class RecentWorkspaceStore:
    """Remember only paths the user explicitly opened in the desktop app."""

    def __init__(self, path: str | Path | None = None, limit: int = 8):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
        self.path = Path(path) if path is not None else base / "zxn-coding-agent" / "gui.json"
        self.limit = max(1, limit)

    def load(self) -> list[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        raw = value.get("recent_workspaces") if isinstance(value, dict) else None
        if not isinstance(raw, list):
            return []
        paths: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            resolved = Path(item).expanduser().resolve()
            text = str(resolved)
            if resolved.is_dir() and text not in paths:
                paths.append(text)
        return paths[: self.limit]

    def remember(self, workspace: str | Path) -> list[str]:
        resolved = resolve_workspace(workspace)
        value = str(resolved)
        recent = [value, *(path for path in self.load() if path != value)][: self.limit]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"recent_workspaces": recent}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        return recent


def resolve_workspace(workspace: str | Path) -> Path:
    resolved = Path(workspace).expanduser().resolve()
    if not resolved.is_dir():
        raise WorkspaceDataError(f"Workspace does not exist: {resolved}")
    return resolved


def switch_workspace(workspace: str | Path, *, running: bool) -> Path:
    if running:
        raise WorkspaceDataError("Stop the current Agent task before switching workspace.")
    return resolve_workspace(workspace)


def project_files(
    workspace: str | Path,
    *,
    query: str = "",
    limit: int = MAX_PROJECT_FILES,
) -> list[str]:
    root = resolve_workspace(workspace)
    needle = query.casefold().strip()
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in NOISE_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath, name)
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if needle and needle not in relative.casefold():
                continue
            found.append(relative)
            if len(found) >= max(1, limit):
                return found
    return found


def project_entries(
    workspace: str | Path,
    *,
    query: str = "",
    limit: int = MAX_PROJECT_FILES,
) -> list[ProjectEntry]:
    """List visible files and directories, including empty directories."""

    root = resolve_workspace(workspace)
    needle = query.casefold().strip()
    found: list[ProjectEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in NOISE_DIRS)
        candidates = [(name, True) for name in dirnames]
        candidates.extend((name, False) for name in sorted(filenames))
        for name, is_directory in candidates:
            path = Path(dirpath, name)
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if needle and needle not in relative.casefold():
                continue
            found.append(ProjectEntry(relative, is_directory))
            if len(found) >= max(1, limit):
                return found
    return found


def read_workspace_text(
    workspace: str | Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_FILE_PREVIEW_BYTES,
) -> tuple[str, bool]:
    root = resolve_workspace(workspace)
    target = (root / relative_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceDataError("File path escapes the workspace.") from exc
    if not target.is_file():
        raise WorkspaceDataError(f"File does not exist: {relative_path}")
    with target.open("rb") as stream:
        data = stream.read(max_bytes + 1)
    if b"\x00" in data[:4096]:
        raise WorkspaceDataError("Binary files are not shown in the text preview.")
    truncated = len(data) > max_bytes
    payload = data[:max_bytes]
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkspaceDataError("File preview requires UTF-8 text.") from exc
    return text, truncated
