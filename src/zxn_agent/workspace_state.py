"""Bounded workspace snapshots and last-known file conflict detection."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

NOISE_DIRS = {
    ".agent",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
NOISE_FILES = {".coverage"}
IGNORE_CONFIG_FILES = {".gitignore", ".agentignore"}
MAX_IGNORE_FILE_BYTES = 256 * 1024
MAX_SNAPSHOT_FILES = 20_000
MAX_SNAPSHOT_FILE_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_HASH_BYTES = 256 * 1024 * 1024
ABSENT = "<absent>"


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int
    ctime_ns: int
    device: int
    inode: int
    digest: str | None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    files: dict[str, FileStamp]
    complete: bool
    note: str | None = None
    hashed_files: int = 0
    reused_files: int = 0
    ignored_entries: int = 0


@dataclass(frozen=True)
class WorkspaceDelta:
    paths: tuple[str, ...]
    complete: bool
    note: str | None = None


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str
    negated: bool = False
    directory_only: bool = False
    anchored: bool = False


def _match_segments(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Match slash-separated gitignore-style segments without crossing on ``*``."""

    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        remaining = pattern[1:]
        if not remaining:
            return True
        return any(_match_segments(path[index:], remaining) for index in range(len(path) + 1))
    return bool(
        path
        and fnmatch.fnmatchcase(path[0], head)
        and _match_segments(path[1:], pattern[1:])
    )


def _rule_matches(rule: IgnoreRule, relative: str, is_dir: bool) -> bool:
    if rule.directory_only and not is_dir:
        return False
    path_parts = tuple(part for part in relative.split("/") if part)
    if not rule.anchored:
        return bool(path_parts and fnmatch.fnmatchcase(path_parts[-1], rule.pattern))
    pattern_parts = tuple(part for part in rule.pattern.split("/") if part)
    return _match_segments(path_parts, pattern_parts)


@dataclass(frozen=True)
class IgnoreRules:
    rules: tuple[IgnoreRule, ...] = ()

    def decision(self, relative: str, is_dir: bool) -> bool | None:
        decision = None
        for rule in self.rules:
            if _rule_matches(rule, relative, is_dir):
                decision = not rule.negated
        return decision


@dataclass(frozen=True)
class WorkspaceIgnore:
    """Pinned ignore policy; tracked Git files can never be hidden by patterns."""

    git_rules: IgnoreRules = IgnoreRules()
    agent_rules: IgnoreRules = IgnoreRules()
    git_tracked: frozenset[str] | None = None

    def _pattern_decision(self, relative: str, is_dir: bool) -> bool | None:
        decision = None
        if self.git_tracked is not None:
            decision = self.git_rules.decision(relative, is_dir)
        agent_decision = self.agent_rules.decision(relative, is_dir)
        return agent_decision if agent_decision is not None else decision

    def ignored(self, relative: str, is_dir: bool) -> bool:
        parts = tuple(part for part in relative.split("/") if part)
        if not parts or parts[-1] in IGNORE_CONFIG_FILES:
            return False
        if any(part in NOISE_DIRS for part in parts):
            return True
        if not is_dir and parts[-1] in NOISE_FILES:
            return True

        tracked = self.git_tracked or frozenset()
        if relative in tracked:
            return False

        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            prefix_is_dir = index < len(parts) or is_dir
            if self._pattern_decision(prefix, prefix_is_dir) is not True:
                continue
            if is_dir and index == len(parts):
                marker = prefix + "/"
                if any(path.startswith(marker) for path in tracked):
                    # Walk the directory so explicitly tracked descendants remain visible.
                    return False
            return True
        return False


def _parse_ignore_file(path: Path) -> IgnoreRules:
    try:
        data = path.read_bytes()
    except OSError:
        return IgnoreRules()
    if len(data) > MAX_IGNORE_FILE_BYTES or b"\x00" in data:
        return IgnoreRules()

    rules: list[IgnoreRule] = []
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        escaped_marker = line.startswith((r"\#", r"\!"))
        if escaped_marker:
            line = line[1:]
        negated = not escaped_marker and line.startswith("!")
        if negated:
            line = line[1:]
        directory_only = line.endswith("/")
        if directory_only:
            line = line[:-1]
        anchored = line.startswith("/") or "/" in line
        pattern = line.lstrip("/")
        if pattern:
            rules.append(IgnoreRule(pattern, negated, directory_only, anchored))
    return IgnoreRules(tuple(rules))


def _git_tracked_paths(root: Path) -> frozenset[str] | None:
    """Return tracked paths relative to the workspace, or None outside Git."""

    try:
        root_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if root_result.returncode != 0:
            return None
        repo_root = Path(os.fsdecode(root_result.stdout).strip()).resolve()
        tracked_result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if tracked_result.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    tracked: set[str] = set()
    for raw in tracked_result.stdout.split(b"\0"):
        if not raw:
            continue
        candidate = (repo_root / os.fsdecode(raw)).resolve(strict=False)
        try:
            tracked.add(candidate.relative_to(root).as_posix())
        except ValueError:
            continue
    return frozenset(tracked)


def load_workspace_ignore(root: str | Path) -> WorkspaceIgnore:
    """Load a conservative ignore policy once for a Runtime process."""

    base = Path(root).resolve()
    tracked = _git_tracked_paths(base)
    return WorkspaceIgnore(
        git_rules=_parse_ignore_file(base / ".gitignore"),
        agent_rules=_parse_ignore_file(base / ".agentignore"),
        git_tracked=tracked,
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp_marker(stamp: FileStamp | None) -> str:
    if stamp is None:
        return ABSENT
    return stamp.digest or (
        f"stat:{stamp.size}:{stamp.mtime_ns}:{stamp.ctime_ns}:"
        f"{stamp.device}:{stamp.inode}"
    )


def _different(before: FileStamp, after: FileStamp) -> bool:
    if before.digest is not None and after.digest is not None:
        return before.digest != after.digest
    return (
        before.size,
        before.mtime_ns,
        before.ctime_ns,
        before.device,
        before.inode,
    ) != (
        after.size,
        after.mtime_ns,
        after.ctime_ns,
        after.device,
        after.inode,
    )


def _stamp(stat: os.stat_result, digest: str | None) -> FileStamp:
    return FileStamp(
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_dev,
        stat.st_ino,
        digest,
    )


def _metadata_unchanged(previous: FileStamp, stat: os.stat_result) -> bool:
    return (
        previous.digest is not None
        and previous.size == stat.st_size
        and previous.mtime_ns == stat.st_mtime_ns
        and previous.ctime_ns == stat.st_ctime_ns
        and previous.device == stat.st_dev
        and previous.inode == stat.st_ino
    )


def _read_digest(path: Path) -> tuple[bytes, os.stat_result]:
    data = path.read_bytes()
    return data, path.stat()


def capture_workspace(
    root: str | Path,
    previous: WorkspaceSnapshot | None = None,
    ignore: WorkspaceIgnore | None = None,
) -> WorkspaceSnapshot:
    """Capture state, reusing digests only when strong file metadata is unchanged."""

    base = Path(root).resolve()
    ignore = ignore or load_workspace_ignore(base)
    files: dict[str, FileStamp] = {}
    complete = True
    notes: set[str] = set()
    hashed_bytes = 0
    hashed_files = 0
    reused_files = 0
    ignored_entries = 0
    stop = False

    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            relative_dir = (Path(dirpath, name).relative_to(base)).as_posix()
            if ignore.ignored(relative_dir, is_dir=True):
                ignored_entries += 1
            else:
                kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            lexical = Path(dirpath, name)
            lexical_relative = lexical.relative_to(base).as_posix()
            if ignore.ignored(lexical_relative, is_dir=False):
                ignored_entries += 1
                continue
            if len(files) >= MAX_SNAPSHOT_FILES:
                complete = False
                notes.add(f"file limit {MAX_SNAPSHOT_FILES} reached")
                stop = True
                break
            path = Path(dirpath) / name
            try:
                resolved = path.resolve(strict=False)
                resolved.relative_to(base)
                stat = resolved.stat()
                relative = resolved.relative_to(base).as_posix()
                old = previous.files.get(relative) if previous is not None else None
                if old is not None and _metadata_unchanged(old, stat):
                    files[relative] = _stamp(stat, old.digest)
                    reused_files += 1
                    continue
                digest = None
                if (
                    stat.st_size <= MAX_SNAPSHOT_FILE_BYTES
                    and hashed_bytes + stat.st_size <= MAX_SNAPSHOT_HASH_BYTES
                ):
                    data, after_read = _read_digest(resolved)
                    hashed_files += 1
                    hashed_bytes += len(data)
                    if (
                        len(data) != after_read.st_size
                        or stat.st_mtime_ns != after_read.st_mtime_ns
                        or stat.st_ctime_ns != after_read.st_ctime_ns
                        or stat.st_dev != after_read.st_dev
                        or stat.st_ino != after_read.st_ino
                    ):
                        complete = False
                        notes.add("a file changed while it was being captured")
                    else:
                        digest = _digest(data)
                    stat = after_read
                else:
                    complete = False
                    notes.add("hash byte budget reached; large files use metadata only")
                files[relative] = _stamp(stat, digest)
            except (OSError, ValueError):
                complete = False
                notes.add("one or more files could not be captured")
        if stop:
            break

    return WorkspaceSnapshot(
        files=files,
        complete=complete,
        note="; ".join(sorted(notes)) if notes else None,
        hashed_files=hashed_files,
        reused_files=reused_files,
        ignored_entries=ignored_entries,
    )


def compare_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> WorkspaceDelta:
    """Return added, removed, or content-changed paths between two snapshots."""

    changed: list[str] = []
    for path in sorted(set(before.files) | set(after.files)):
        old = before.files.get(path)
        new = after.files.get(path)
        if old is None or new is None or _different(old, new):
            changed.append(path)
    notes = [note for note in (before.note, after.note) if note]
    return WorkspaceDelta(
        paths=tuple(changed),
        complete=before.complete and after.complete,
        note="; ".join(dict.fromkeys(notes)) if notes else None,
    )


def snapshot_fingerprint(snapshot: WorkspaceSnapshot) -> str:
    """Return a deterministic identity for the captured workspace state."""

    digest = hashlib.sha256()
    digest.update(b"complete\0" if snapshot.complete else b"partial\0")
    for path, stamp in sorted(snapshot.files.items()):
        marker = _stamp_marker(stamp)
        digest.update(path.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(marker.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class WorkspaceTracker:
    """Track workspace transitions and prevent stale file overwrites.

    Snapshot state is deliberately process-local. Session resume already
    invalidates verification because the workspace may have changed while the
    process was stopped.
    """

    root: Path | None = None
    last_snapshot: WorkspaceSnapshot | None = None
    known: dict[str, str] = field(default_factory=dict)
    known_absent: set[str] = field(default_factory=set)
    unverified: set[str] = field(default_factory=set)
    reported: dict[str, str] = field(default_factory=dict)
    baseline_complete: bool = False
    ignore_policy: WorkspaceIgnore | None = field(default=None, repr=False)

    def _set_root(self, root: str | Path) -> Path:
        resolved = Path(root).resolve()
        if self.root is not None and self.root != resolved:
            self.last_snapshot = None
            self.known.clear()
            self.known_absent.clear()
            self.unverified.clear()
            self.reported.clear()
            self.baseline_complete = False
            self.ignore_policy = None
        self.root = resolved
        return resolved

    def _relative(self, path: Path) -> str:
        if self.root is None:
            raise RuntimeError("workspace tracker has no root")
        return path.resolve(strict=False).relative_to(self.root).as_posix()

    def initialize(
        self,
        root: str | Path,
        *,
        require_file_observation: bool = False,
    ) -> WorkspaceSnapshot:
        """Establish the process baseline used by shell and edit checks."""

        base = self._set_root(root)
        self.ignore_policy = load_workspace_ignore(base)
        snapshot = capture_workspace(base, ignore=self.ignore_policy)
        self.last_snapshot = snapshot
        self.known = (
            {}
            if require_file_observation
            else {
                path: stamp.digest
                for path, stamp in snapshot.files.items()
                if stamp.digest is not None
            }
        )
        self.unverified = (
            set(snapshot.files)
            if require_file_observation
            else {path for path, stamp in snapshot.files.items() if stamp.digest is None}
        )
        self.known_absent.clear()
        self.reported.clear()
        self.baseline_complete = snapshot.complete
        return snapshot

    def _ensure_command_baseline(self, root: str | Path) -> WorkspaceSnapshot:
        base = self._set_root(root)
        if self.last_snapshot is None:
            return self.initialize(base)
        return capture_workspace(
            base,
            previous=self.last_snapshot,
            ignore=self.ignore_policy,
        )

    def _mark_reported(
        self,
        paths: tuple[str, ...],
        snapshot: WorkspaceSnapshot,
    ) -> None:
        for path in paths:
            self.reported[path] = _stamp_marker(snapshot.files.get(path))

    def before_command(self, root: str | Path) -> tuple[WorkspaceSnapshot, WorkspaceDelta]:
        """Detect changes that occurred since the Runtime's last snapshot."""

        return self.reconcile(root)

    def reconcile(self, root: str | Path) -> tuple[WorkspaceSnapshot, WorkspaceDelta]:
        """Refresh the workspace and report changes since the last Runtime view."""

        if self.last_snapshot is None:
            snapshot = self._ensure_command_baseline(root)
            return snapshot, WorkspaceDelta((), snapshot.complete, snapshot.note)
        base = self._set_root(root)
        current = capture_workspace(
            base,
            previous=self.last_snapshot,
            ignore=self.ignore_policy,
        )
        delta = compare_snapshots(self.last_snapshot, current)
        self._mark_reported(delta.paths, current)
        self.last_snapshot = current
        self.baseline_complete = self.baseline_complete and current.complete
        return current, delta

    def fingerprint(self) -> str | None:
        """Return the identity of the latest snapshot, if tracking has started."""

        return (
            snapshot_fingerprint(self.last_snapshot)
            if self.last_snapshot is not None
            else None
        )

    def after_command(
        self,
        before: WorkspaceSnapshot,
    ) -> tuple[WorkspaceSnapshot, WorkspaceDelta]:
        """Detect changes caused while a shell command was running."""

        if self.root is None:
            raise RuntimeError("workspace tracker has no root")
        current = capture_workspace(
            self.root,
            previous=before,
            ignore=self.ignore_policy,
        )
        delta = compare_snapshots(before, current)
        self._mark_reported(delta.paths, current)
        self.last_snapshot = current
        self.baseline_complete = self.baseline_complete and current.complete
        return current, delta

    def _update_snapshot_path(self, relative: str, path: Path, data: bytes | None) -> None:
        if self.last_snapshot is None:
            return
        files = dict(self.last_snapshot.files)
        ignored = bool(
            data is not None
            and self.ignore_policy is not None
            and self.ignore_policy.ignored(relative, is_dir=False)
        )
        if data is None or ignored:
            files.pop(relative, None)
        else:
            try:
                stat = path.stat()
                files[relative] = _stamp(stat, _digest(data))
            except OSError:
                files.pop(relative, None)
        self.last_snapshot = WorkspaceSnapshot(
            files,
            self.last_snapshot.complete,
            self.last_snapshot.note,
            self.last_snapshot.hashed_files,
            self.last_snapshot.reused_files,
            self.last_snapshot.ignored_entries,
        )

    def _expected_conflict(self, relative: str, data: bytes | None) -> str | None:
        marker = ABSENT if data is None else _digest(data)
        if relative in self.known:
            if marker != self.known[relative]:
                return "file content changed after the Runtime last observed it"
            return None
        if relative in self.known_absent:
            if marker != ABSENT:
                return "file was created after the Runtime last observed it as absent"
            return None
        if relative in self.unverified:
            return "this process requires a current read observation before editing the file"
        if self.last_snapshot is not None and self.baseline_complete and data is not None:
            return "file was created after the Runtime established its workspace baseline"
        return None

    def check_edit(self, path: Path, data: bytes | None) -> tuple[str | None, bool]:
        """Return a stale-write reason and whether this exact change is newly seen."""

        if self.root is None:
            self._set_root(path.parent)
        relative = self._relative(path)
        reason = self._expected_conflict(relative, data)
        awaiting_observation = relative in self.unverified
        if reason is None:
            if relative not in self.known and relative not in self.known_absent:
                self.accept(path, data)
            return None, False
        marker = ABSENT if data is None else _digest(data)
        newly_seen = not awaiting_observation and self.reported.get(relative) != marker
        self.reported[relative] = marker
        self._update_snapshot_path(relative, path, data)
        return reason, newly_seen

    def observe(self, path: Path, data: bytes | None) -> bool:
        """Accept a read observation and report a newly discovered outside change."""

        if self.root is None:
            self._set_root(path.parent)
        relative = self._relative(path)
        reason = self._expected_conflict(relative, data)
        awaiting_observation = relative in self.unverified
        marker = ABSENT if data is None else _digest(data)
        newly_seen = (
            reason is not None
            and not awaiting_observation
            and self.reported.get(relative) != marker
        )
        self.accept(path, data)
        return newly_seen

    def accept(self, path: Path, data: bytes | None) -> None:
        """Record content that was read or deliberately written by the Runtime."""

        if self.root is None:
            self._set_root(path.parent)
        relative = self._relative(path)
        self.unverified.discard(relative)
        self.reported.pop(relative, None)
        if data is None:
            self.known.pop(relative, None)
            self.known_absent.add(relative)
        else:
            self.known[relative] = _digest(data)
            self.known_absent.discard(relative)
        self._update_snapshot_path(relative, path, data)
