"""Append-only, resumable local sessions kept separate from audit trajectories."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ctx import Ctx
from log import redact
from state import State

SESSION_VERSION = 1


class SessionError(RuntimeError):
    """Raised when a requested session cannot be safely loaded."""


@dataclass(frozen=True)
class LoadedSession:
    ctx: Ctx
    state: dict[str, Any]
    previous_verified_revision: int
    original_model: str


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _state_data(st: State) -> dict[str, Any]:
    return {
        "rev": st.rev,
        "ok_rev": st.ok_rev,
        "changed": st.changed,
        "files": sorted(st.files),
        "in_tok": st.in_tok,
        "out_tok": st.out_tok,
        "last_check_cmd": st.last_check_cmd,
        "last_check_rc": st.last_check_rc,
        "last_check_rev": st.last_check_rev,
        "external_change_possible": st.external_change_possible,
    }


def restore_state(data: dict[str, Any], session_id: str) -> State:
    """Restore durable progress but deliberately reset process-local safety state."""

    return State(
        rev=max(0, int(data.get("rev", 0))),
        # Verification never survives a process boundary: files may have
        # changed while the Agent was stopped.
        ok_rev=-1,
        changed=bool(data.get("changed", False)),
        files={str(path) for path in data.get("files", []) if isinstance(path, str)},
        in_tok=max(0, int(data.get("in_tok", 0))),
        out_tok=max(0, int(data.get("out_tok", 0))),
        last_check_cmd=(
            str(data["last_check_cmd"])
            if data.get("last_check_cmd") is not None
            else None
        ),
        last_check_rc=(
            int(data["last_check_rc"])
            if data.get("last_check_rc") is not None
            else None
        ),
        last_check_rev=(
            int(data["last_check_rev"])
            if data.get("last_check_rev") is not None
            else None
        ),
        external_change_possible=bool(data.get("external_change_possible", False)),
        session_id=session_id,
    )


class SessionStore:
    """A small linear JSONL session with full logical message groups."""

    def __init__(self, path: Path, session_id: str, workspace: Path):
        self.path = path
        self.session_id = session_id
        self.workspace = workspace
        self.secret = os.environ.get("AGENT_API_KEY", "")

    @staticmethod
    def directory(workspace: str | Path) -> Path:
        return Path(workspace).resolve() / ".agent" / "sessions"

    @classmethod
    def create(cls, workspace: str | Path, model: str, task: str) -> SessionStore:
        root = Path(workspace).resolve()
        folder = cls.directory(root)
        folder.mkdir(parents=True, exist_ok=True)
        session_id = uuid.uuid4().hex[:8]
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        path = folder / f"session-{stamp}-{session_id}.jsonl"
        store = cls(path, session_id, root)
        store._append({
            "type": "session",
            "version": SESSION_VERSION,
            "id": session_id,
            "created": _now(),
            "workspace": str(root),
            "model": model,
        })
        store.record_task(task)
        return store

    @classmethod
    def open(cls, workspace: str | Path, selector: str = "latest") -> SessionStore:
        root = Path(workspace).resolve()
        folder = cls.directory(root)
        candidates = sorted(
            folder.glob("session-*.jsonl") if folder.is_dir() else [],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise SessionError(f"No sessions found for workspace {root}.")
        if selector in {"", "latest"}:
            path = candidates[0]
        else:
            safe_selector = Path(selector).name
            matches = [
                path
                for path in candidates
                if path.name == safe_selector
                or path.stem == safe_selector
                or path.stem.endswith(f"-{safe_selector}")
            ]
            if not matches:
                raise SessionError(f"Session {selector!r} was not found in {folder}.")
            if len(matches) > 1:
                raise SessionError(f"Session selector {selector!r} is ambiguous.")
            path = matches[0]

        header = cls._read_entries(path)[0]
        if header.get("type") != "session":
            raise SessionError(f"Invalid session header in {path.name}.")
        session_workspace = Path(str(header.get("workspace", ""))).resolve()
        if session_workspace != root:
            raise SessionError("Session workspace does not match the requested workspace.")
        return cls(path, str(header.get("id", "")), root)

    def _append(self, entry: dict[str, Any]) -> None:
        safe = redact(entry, self.secret)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(safe, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_task(self, task: str) -> None:
        self._append({"type": "task", "time": _now(), "text": task})

    def record_group(self, messages: list[dict], st: State) -> None:
        self._append({
            "type": "group",
            "time": _now(),
            "messages": messages,
            "state": _state_data(st),
        })

    def record_between_turn(self, messages: list[dict], st: State) -> None:
        self._append({
            "type": "between_turn",
            "time": _now(),
            "messages": messages,
            "state": _state_data(st),
        })

    def record_state(self, st: State, reason: str) -> None:
        self._append({
            "type": "state",
            "time": _now(),
            "reason": reason,
            "state": _state_data(st),
        })

    @staticmethod
    def _read_entries(path: Path) -> list[dict[str, Any]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        entries: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                # A killed process can leave one incomplete final line. Earlier
                # corruption is not ignored because it could reorder history.
                if index == len(lines) - 1:
                    break
                raise SessionError(
                    f"Invalid JSON in {path.name} at line {index + 1}."
                ) from exc
            if not isinstance(entry, dict):
                raise SessionError(f"Invalid entry in {path.name} at line {index + 1}.")
            entries.append(entry)
        if not entries:
            raise SessionError(f"Session {path.name} is empty.")
        return entries

    def load(self, system_prompt: str) -> LoadedSession:
        entries = self._read_entries(self.path)
        header = entries[0]
        if header.get("type") != "session" or header.get("version") != SESSION_VERSION:
            raise SessionError(f"Unsupported session format in {self.path.name}.")

        ctx: Ctx | None = None
        latest_state: dict[str, Any] = {}
        for entry in entries[1:]:
            kind = entry.get("type")
            if kind == "task":
                text = entry.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise SessionError("Session contains an invalid task entry.")
                if ctx is None:
                    ctx = Ctx(system_prompt, text)
                else:
                    ctx.start_task(text)
            elif kind in {"group", "between_turn"}:
                if ctx is None:
                    raise SessionError("Session message group appeared before the first task.")
                messages = entry.get("messages")
                if not isinstance(messages, list) or not all(
                    isinstance(message, dict) for message in messages
                ):
                    raise SessionError("Session contains an invalid logical group.")
                if kind == "group":
                    ctx.add_group(messages)
                else:
                    ctx.add_between_turn_group(messages)
                if isinstance(entry.get("state"), dict):
                    latest_state = entry["state"]
            elif kind == "state" and isinstance(entry.get("state"), dict):
                latest_state = entry["state"]

        if ctx is None:
            raise SessionError("Session does not contain a user task.")
        return LoadedSession(
            ctx=ctx,
            state=latest_state,
            previous_verified_revision=int(latest_state.get("ok_rev", -1)),
            original_model=str(header.get("model", "")),
        )

    @classmethod
    def summaries(cls, workspace: str | Path, limit: int = 10) -> list[dict[str, Any]]:
        folder = cls.directory(workspace)
        candidates = sorted(
            folder.glob("session-*.jsonl") if folder.is_dir() else [],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:limit]
        summaries = []
        for path in candidates:
            try:
                entries = cls._read_entries(path)
                header = entries[0]
                first_task = next(
                    (entry.get("text", "") for entry in entries if entry.get("type") == "task"),
                    "",
                )
                task_count = sum(entry.get("type") == "task" for entry in entries)
                summaries.append({
                    "id": header.get("id", ""),
                    "path": path,
                    "task": str(first_task)[:80],
                    "tasks": task_count,
                    "updated": datetime.fromtimestamp(path.stat().st_mtime).astimezone(),
                })
            except (OSError, SessionError):
                continue
        return summaries
