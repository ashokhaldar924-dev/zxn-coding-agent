"""Read-only snapshot of files that were dirty before an agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess


@dataclass
class GitGuard:
    repo_root: Path | None = None
    initial_dirty: set[Path] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return self.repo_root is not None

    def is_initially_dirty(self, path: Path) -> bool:
        return path.resolve(strict=False) in self.initial_dirty

    def display_paths(self, workspace: str | Path) -> list[str]:
        base = Path(workspace).resolve()
        shown = []
        for path in sorted(self.initial_dirty, key=lambda item: str(item).lower()):
            try:
                shown.append(path.relative_to(base).as_posix())
            except ValueError:
                shown.append(str(path))
        return shown

    @classmethod
    def scan(cls, workspace: str | Path) -> "GitGuard":
        """Capture Git's initial dirty set; silently disable outside a repository."""

        cwd = str(Path(workspace).resolve())
        try:
            root_result = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if root_result.returncode != 0:
                return cls()
            repo_root = Path(root_result.stdout.strip()).resolve()
            status_result = subprocess.run(
                ["git", "-C", str(repo_root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if status_result.returncode != 0:
                return cls(repo_root=repo_root)
        except (OSError, subprocess.SubprocessError):
            return cls()

        dirty: set[Path] = set()
        records = status_result.stdout.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            if not record:
                index += 1
                continue
            status = record[:2]
            path_text = record[3:] if len(record) >= 4 else ""
            if path_text:
                dirty.add((repo_root / path_text).resolve(strict=False))
            index += 1
            if "R" in status or "C" in status:
                index += 1
        return cls(repo_root=repo_root, initial_dirty=dirty)
