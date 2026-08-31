"""Conflict-aware before-images for edits performed by Agent file tools."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .changes import FileChange, FileDiff, summarize_byte_change, unified_byte_diff


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be created or safely restored."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class PreparedCheckpoint:
    checkpoint_id: str
    path: str
    existed: bool
    before_hash: str | None
    after_hash: str
    before_blob: str | None
    revision_before: int


@dataclass(frozen=True)
class RestoreResult:
    checkpoint_id: str
    path: str
    deleted_created_file: bool


class CheckpointManager:
    """Track only direct write_file/edit_file effects for one durable session."""

    def __init__(self, workspace: str | Path, session_id: str):
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id
        self.folder = self.workspace / ".agent" / "checkpoints" / session_id
        self.blobs = self.folder / "blobs"
        self.manifest = self.folder / "manifest.jsonl"

    def _safe_target(self, relative_path: str) -> Path:
        target = (self.workspace / relative_path).resolve(strict=False)
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise CheckpointError(f"checkpoint path escapes workspace: {relative_path!r}") from exc
        return target

    def _append(self, entry: dict[str, Any]) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.manifest.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _entries(self) -> list[dict[str, Any]]:
        if not self.manifest.is_file():
            return []
        entries = []
        lines = self.manifest.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    break
                raise CheckpointError(
                    f"checkpoint manifest is corrupt at line {index + 1}"
                ) from exc
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def prepare(
        self,
        relative_path: str,
        before: bytes | None,
        after: bytes,
        revision_before: int,
    ) -> PreparedCheckpoint:
        """Persist the before-image before the target file is touched."""

        self._safe_target(relative_path)
        existing = [entry for entry in self._entries() if entry.get("type") == "prepare"]
        checkpoint_id = f"cp-{len(existing) + 1:04d}-{uuid.uuid4().hex[:6]}"
        before_hash = _sha(before) if before is not None else None
        before_blob = None
        if before is not None:
            self.blobs.mkdir(parents=True, exist_ok=True)
            before_blob = before_hash
            blob_path = self.blobs / before_blob
            if not blob_path.exists():
                blob_path.write_bytes(before)

        prepared = PreparedCheckpoint(
            checkpoint_id=checkpoint_id,
            path=relative_path,
            existed=before is not None,
            before_hash=before_hash,
            after_hash=_sha(after),
            before_blob=before_blob,
            revision_before=revision_before,
        )
        self._append({
            "type": "prepare",
            "time": _now(),
            "id": prepared.checkpoint_id,
            "path": prepared.path,
            "existed": prepared.existed,
            "before_hash": prepared.before_hash,
            "after_hash": prepared.after_hash,
            "before_blob": prepared.before_blob,
            "revision_before": prepared.revision_before,
        })
        return prepared

    def commit(self, prepared: PreparedCheckpoint, revision_after: int) -> None:
        self._append({
            "type": "applied",
            "time": _now(),
            "id": prepared.checkpoint_id,
            "revision_after": revision_after,
        })

    def _records(self) -> list[dict[str, Any]]:
        prepared: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        applied: dict[str, Any] = {}
        restored: set[str] = set()
        for entry in self._entries():
            checkpoint_id = str(entry.get("id", ""))
            if entry.get("type") == "prepare" and checkpoint_id:
                prepared[checkpoint_id] = entry
                order.append(checkpoint_id)
            elif entry.get("type") == "applied":
                applied[checkpoint_id] = entry.get("revision_after")
            elif entry.get("type") == "restored":
                restored.add(checkpoint_id)

        records = []
        for checkpoint_id in order:
            if checkpoint_id in restored:
                continue
            record = prepared[checkpoint_id]
            target = self._safe_target(str(record["path"]))
            current_hash = _sha(target.read_bytes()) if target.is_file() else None
            # A crash after the target write but before the applied marker is
            # recoverable when the on-disk hash proves that the edit landed.
            if checkpoint_id in applied or current_hash == record.get("after_hash"):
                active_record = dict(record)
                revision_after = applied.get(checkpoint_id)
                if revision_after is None:
                    revision_after = int(record.get("revision_before", 0)) + 1
                try:
                    active_record["revision_after"] = max(1, int(revision_after))
                except (TypeError, ValueError) as exc:
                    raise CheckpointError(
                        f"checkpoint {checkpoint_id} has an invalid revision"
                    ) from exc
                records.append(active_record)
        return records

    def active(self) -> list[dict[str, Any]]:
        return list(self._records())

    def change_summaries(self, start_index: int = 0) -> list[FileChange]:
        """Summarize active direct Agent edits against their earliest before-images."""

        if start_index < 0:
            raise ValueError("start_index must be >= 0")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in self._records()[start_index:]:
            grouped.setdefault(str(record["path"]), []).append(record)

        summaries: list[FileChange] = []
        for path, records in grouped.items():
            first = records[0]
            latest = records[-1]
            target = self._safe_target(path)
            current = target.read_bytes() if target.is_file() else None
            current_hash = _sha(current) if current is not None else None
            if current_hash != latest.get("after_hash"):
                # A later shell/user change means the Agent checkpoint can no
                # longer provide truthful net line counts for the current file.
                continue
            before = None
            if bool(first.get("existed")):
                blob = self.blobs / str(first.get("before_blob"))
                if not blob.is_file():
                    continue
                before = blob.read_bytes()
                if _sha(before) != first.get("before_hash"):
                    continue
            summaries.append(summarize_byte_change(path, before, current))
        return summaries

    def file_diff(
        self,
        relative_path: str,
        *,
        start_index: int = 0,
        max_chars: int = 60_000,
    ) -> FileDiff:
        """Return a bounded diff proven by checkpoint before/after hashes."""

        if start_index < 0:
            raise ValueError("start_index must be >= 0")
        records = [
            record
            for record in self._records()[start_index:]
            if str(record.get("path")) == relative_path
        ]
        if not records:
            raise CheckpointError(
                f"No direct Agent file checkpoint is available for {relative_path!r}."
            )
        first, latest = records[0], records[-1]
        target = self._safe_target(relative_path)
        after = target.read_bytes() if target.is_file() else None
        after_hash = _sha(after) if after is not None else None
        if after_hash != latest.get("after_hash"):
            raise CheckpointError(
                f"Cannot show a trusted diff for {relative_path}: the file changed "
                "after the latest Agent checkpoint."
            )
        before = None
        if bool(first.get("existed")):
            blob = self.blobs / str(first.get("before_blob"))
            if not blob.is_file():
                raise CheckpointError(f"Before-image for {relative_path!r} is missing.")
            before = blob.read_bytes()
            if _sha(before) != first.get("before_hash"):
                raise CheckpointError(f"Before-image for {relative_path!r} failed its hash check.")
        return unified_byte_diff(
            relative_path,
            before,
            after,
            max_chars=max_chars,
        )

    def restore_since(self, start_index: int = 0) -> list[RestoreResult]:
        """Restore one task's checkpoints after preflighting every touched file."""

        if start_index < 0:
            raise ValueError("start_index must be >= 0")
        records = self._records()[start_index:]
        if not records:
            raise CheckpointError("No restorable Agent file checkpoint exists for this task.")

        latest_by_path: dict[str, dict[str, Any]] = {}
        for record in records:
            latest_by_path[str(record["path"])] = record
            if bool(record.get("existed")):
                blob = self.blobs / str(record.get("before_blob"))
                if not blob.is_file():
                    raise CheckpointError(f"Before-image for {record['id']} is missing.")
                before = blob.read_bytes()
                if _sha(before) != record.get("before_hash"):
                    raise CheckpointError(
                        f"Before-image for {record['id']} failed its hash check."
                    )

        # Check every final file state before changing any file. This prevents
        # a known external edit from causing a partial multi-file restore.
        for path, record in latest_by_path.items():
            target = self._safe_target(path)
            current_hash = _sha(target.read_bytes()) if target.is_file() else None
            if current_hash != record.get("after_hash"):
                raise CheckpointError(
                    f"Refusing to restore task changes: {path} changed after the "
                    "latest Agent edit. Preserve or reconcile the newer user change first."
                )

        return [self.restore(str(record["id"])) for record in reversed(records)]

    def restore(self, checkpoint_id: str | None = None) -> RestoreResult:
        records = self._records()
        if not records:
            raise CheckpointError("No restorable Agent file checkpoint is available.")
        if checkpoint_id:
            matches = [record for record in records if record.get("id") == checkpoint_id]
            if not matches:
                raise CheckpointError(f"Active checkpoint {checkpoint_id!r} was not found.")
            record = matches[0]
        else:
            record = records[-1]

        target = self._safe_target(str(record["path"]))
        current_hash = _sha(target.read_bytes()) if target.is_file() else None
        if current_hash != record.get("after_hash"):
            raise CheckpointError(
                f"Refusing to restore {record['path']}: it changed after the Agent edit. "
                "Preserve or reconcile the newer user change first."
            )

        if bool(record.get("existed")):
            blob_name = record.get("before_blob")
            blob = self.blobs / str(blob_name)
            if not blob.is_file():
                raise CheckpointError(f"Before-image for {record['id']} is missing.")
            before = blob.read_bytes()
            if _sha(before) != record.get("before_hash"):
                raise CheckpointError(f"Before-image for {record['id']} failed its hash check.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(before)
            deleted = False
        else:
            target.unlink()
            deleted = True

        self._append({"type": "restored", "time": _now(), "id": record["id"]})
        return RestoreResult(
            checkpoint_id=str(record["id"]),
            path=str(record["path"]),
            deleted_created_file=deleted,
        )
