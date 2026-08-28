"""Local command execution and durable large-output storage."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

OUTPUT_ID_RE = re.compile(r"cmd-[0-9a-f]{12}\.txt")


@dataclass(frozen=True)
class CommandExecution:
    text: str
    ok: bool
    rc: int | None = None
    output_ref: str | None = None
    output_chars: int = 0


def _scope(session_id: str | None) -> str:
    if session_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        return session_id
    return "runtime"


def _redact(text: str) -> str:
    secret = os.environ.get("AGENT_API_KEY", "")
    return text.replace(secret, "[REDACTED]") if secret else text


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _render(rc: int | None, stdout: str = "", stderr: str = "", error: str = "") -> str:
    parts = [f"exit code: {rc}" if rc is not None else error]
    if stdout:
        parts.append(f"stdout:\n{stdout.rstrip()}")
    if stderr:
        parts.append(f"stderr:\n{stderr.rstrip()}")
    return _redact("\n".join(part for part in parts if part))


class CommandRunner:
    """Execute commands; keep policy decisions outside this class."""

    def __init__(
        self,
        workspace: str | Path,
        session_id: str | None,
        preview_chars: int,
    ):
        self.workspace = Path(workspace).resolve()
        self.session_id = session_id
        self.preview_chars = max(1, preview_chars)

    @property
    def output_dir(self) -> Path:
        return self.workspace / ".agent" / "outputs" / _scope(self.session_id)

    def _save(self, text: str) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            output_id = f"cmd-{secrets.token_hex(6)}.txt"
            path = self.output_dir / output_id
            try:
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
                return output_id
            except FileExistsError:
                continue
        raise OSError("could not allocate a unique command output id")

    def _preview(self, full: str) -> tuple[str, str | None]:
        if len(full) <= self.preview_chars:
            return full, None
        try:
            output_ref = self._save(full)
            notice = (
                f"\n[output truncated: {len(full)} chars; saved as {output_ref}; "
                "use read_command_output]\n"
            )
        except OSError as exc:
            output_ref = None
            notice = (
                f"\n[output truncated: {len(full)} chars; full-output storage failed: "
                f"{type(exc).__name__}]\n"
            )
        if len(notice) >= self.preview_chars:
            return notice[: self.preview_chars], output_ref
        room = self.preview_chars - len(notice)
        head = room // 2
        tail = room - head
        return full[:head] + notice + (full[-tail:] if tail else ""), output_ref

    def run(self, cmd: str, timeout: float) -> CommandExecution:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
            full = _render(proc.returncode, proc.stdout, proc.stderr)
            preview, output_ref = self._preview(full)
            return CommandExecution(
                preview,
                True,
                rc=proc.returncode,
                output_ref=output_ref,
                output_chars=len(full),
            )
        except subprocess.TimeoutExpired as exc:
            full = _render(
                None,
                _as_text(exc.stdout),
                _as_text(exc.stderr),
                f"Command timed out after {timeout:g} seconds.",
            )
            preview, output_ref = self._preview(full)
            return CommandExecution(
                preview,
                False,
                output_ref=output_ref,
                output_chars=len(full),
            )
        except OSError as exc:
            full = _redact(f"Command runtime error: {type(exc).__name__}: {exc}")
            preview, output_ref = self._preview(full)
            return CommandExecution(
                preview,
                False,
                output_ref=output_ref,
                output_chars=len(full),
            )


def read_saved_output(
    workspace: str | Path,
    session_id: str | None,
    output_id: str,
) -> str:
    """Read one opaque output id from the current session or user-shell scope."""

    if not isinstance(output_id, str) or OUTPUT_ID_RE.fullmatch(output_id) is None:
        raise ValueError("output_id must be an id returned by run_command or check_command")
    root = Path(workspace).resolve() / ".agent" / "outputs"
    scopes = [_scope(session_id)]
    if scopes[0] != "runtime":
        scopes.append("runtime")
    for scope in scopes:
        path = root / scope / output_id
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError(f"saved command output not found: {output_id}")
