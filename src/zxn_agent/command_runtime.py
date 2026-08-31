"""Bounded local command execution and durable large-output storage."""

from __future__ import annotations

import codecs
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO, TextIO

OUTPUT_ID_RE = re.compile(r"cmd-[0-9a-f]{12}\.txt")
STREAM_CHUNK_BYTES = 64 * 1024
TERMINATION_GRACE_SECONDS = 0.75


@dataclass(frozen=True)
class CommandExecution:
    text: str
    ok: bool
    rc: int | None = None
    output_ref: str | None = None
    output_chars: int = 0
    elapsed_seconds: float | None = None
    cancelled: bool = False


class _PreviewCollector:
    """Retain bounded head/tail text while a full result is streamed to disk."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.head = ""
        self.tail = ""
        self.total = 0

    def add(self, text: str) -> None:
        if not text:
            return
        self.total += len(text)
        if len(self.head) < self.limit:
            self.head += text[: self.limit - len(self.head)]
        self.tail = (self.tail + text)[-self.limit :]

    def preview(self, output_id: str) -> str:
        notice = (
            f"\n[output truncated: {self.total} chars; saved as {output_id}; "
            "use read_command_output]\n"
        )
        if len(notice) >= self.limit:
            return notice[: self.limit]
        room = self.limit - len(notice)
        head_size = room // 2
        tail_size = room - head_size
        return (
            self.head[:head_size]
            + notice
            + (self.tail[-tail_size:] if tail_size else "")
        )


def _scope(session_id: str | None) -> str:
    if session_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        return session_id
    return "runtime"


def _redact(text: str) -> str:
    secret = os.environ.get("AGENT_API_KEY", "")
    return text.replace(secret, "[REDACTED]") if secret else text


def _render(rc: int | None, stdout: str = "", stderr: str = "", error: str = "") -> str:
    parts = [f"exit code: {rc}" if rc is not None else error]
    if stdout:
        parts.append(f"stdout:\n{stdout.rstrip()}")
    if stderr:
        parts.append(f"stderr:\n{stderr.rstrip()}")
    return _redact("\n".join(part for part in parts if part))


def _bounded_text(text: str, limit: int, marker: str) -> str:
    if len(text) <= limit:
        return text
    if len(marker) >= limit:
        return marker[:limit]
    room = limit - len(marker)
    head = room // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _child_environment() -> dict[str, str]:
    """Keep the Agent's model credential out of repository-controlled commands."""

    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() != "AGENT_API_KEY"
    }


def _popen_group_options() -> dict[str, int | bool]:
    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {
            "creationflags": int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        }
    return {}


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> str | None:
    """Best-effort termination of the shell and ordinary descendants."""

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return None
        except OSError as exc:
            return f"could not terminate process group: {type(exc).__name__}"
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        # A background child can keep the group alive after the shell exits.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return f"could not kill process group: {type(exc).__name__}"
    elif os.name == "nt":
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    else:
        try:
            process.kill()
        except OSError:
            pass

    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return "timed-out process did not terminate cleanly"
    return None


def _write_piece(stream: TextIO, collector: _PreviewCollector, text: str) -> None:
    stream.write(text)
    collector.add(text)


def _write_redacted_stream(
    source: BinaryIO,
    destination: TextIO,
    collector: _PreviewCollector,
) -> None:
    """Decode and redact one captured stream without loading it into memory."""

    source.flush()
    source.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    secret = os.environ.get("AGENT_API_KEY", "")
    keep = max(0, len(secret) - 1)
    pending = ""

    while True:
        raw = source.read(STREAM_CHUNK_BYTES)
        if not raw:
            break
        pending += decoder.decode(raw, final=False)
        if secret:
            while True:
                index = pending.find(secret)
                if index < 0:
                    break
                _write_piece(destination, collector, pending[:index])
                _write_piece(destination, collector, "[REDACTED]")
                pending = pending[index + len(secret) :]
            if len(pending) > keep:
                split = len(pending) - keep
                _write_piece(destination, collector, pending[:split])
                pending = pending[split:]
        else:
            _write_piece(destination, collector, pending)
            pending = ""

    pending += decoder.decode(b"", final=True)
    if secret:
        pending = pending.replace(secret, "[REDACTED]")
    _write_piece(destination, collector, pending)


def _read_bounded_stream(source: BinaryIO, limit: int) -> str:
    """Return bounded head/tail text for the rare durable-storage failure path."""

    source.flush()
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(0)
    if size <= limit:
        raw = source.read()
    else:
        marker = b"\n... [stream truncated] ...\n"
        room = max(0, limit - len(marker))
        head = room // 2
        tail = room - head
        raw = source.read(head) + marker
        if tail:
            source.seek(-tail, os.SEEK_END)
            raw += source.read(tail)
    return _redact(raw.decode("utf-8", errors="replace"))


class CommandRunner:
    """Execute commands; keep permission and workspace policy outside this class."""

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

    def _allocate_output(self) -> tuple[str, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(10):
            output_id = f"cmd-{secrets.token_hex(6)}.txt"
            path = self.output_dir / output_id
            if not path.exists():
                return output_id, path
        raise OSError("could not allocate a unique command output id")

    @staticmethod
    def _write_metadata(path: Path, total_chars: int) -> None:
        metadata = path.with_suffix(".json")
        try:
            with metadata.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump({"chars": total_chars}, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            # The output remains readable; range reads can count it lazily.
            metadata.unlink(missing_ok=True)

    def _materialize(
        self,
        status: str,
        stdout_buffer: BinaryIO,
        stderr_buffer: BinaryIO,
    ) -> tuple[str, str | None, int]:
        stdout_buffer.seek(0, os.SEEK_END)
        stdout_bytes = stdout_buffer.tell()
        stderr_buffer.seek(0, os.SEEK_END)
        stderr_bytes = stderr_buffer.tell()
        small_upper_bound = (
            len(status)
            + stdout_bytes
            + stderr_bytes
            + (len("\nstdout:\n") if stdout_bytes else 0)
            + (len("\nstderr:\n") if stderr_bytes else 0)
        )
        if small_upper_bound <= self.preview_chars:
            parts = [status]
            if stdout_bytes:
                stdout_buffer.seek(0)
                parts.append(
                    "stdout:\n" + stdout_buffer.read().decode("utf-8", errors="replace")
                )
            if stderr_bytes:
                stderr_buffer.seek(0)
                parts.append(
                    "stderr:\n" + stderr_buffer.read().decode("utf-8", errors="replace")
                )
            full = _redact("\n".join(parts))
            if len(full) <= self.preview_chars:
                return full, None, len(full)

        output_id: str | None = None
        path: Path | None = None
        try:
            output_id, path = self._allocate_output()
            collector = _PreviewCollector(self.preview_chars)
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                _write_piece(stream, collector, status)
                stdout_buffer.seek(0, os.SEEK_END)
                if stdout_buffer.tell():
                    _write_piece(stream, collector, "\nstdout:\n")
                    _write_redacted_stream(stdout_buffer, stream, collector)
                stderr_buffer.seek(0, os.SEEK_END)
                if stderr_buffer.tell():
                    _write_piece(stream, collector, "\nstderr:\n")
                    _write_redacted_stream(stderr_buffer, stream, collector)
                stream.flush()
            if collector.total <= self.preview_chars:
                path.unlink(missing_ok=True)
                return collector.head, None, collector.total
            self._write_metadata(path, collector.total)
            return collector.preview(output_id), output_id, collector.total
        except OSError as exc:
            if path is not None:
                path.unlink(missing_ok=True)
                path.with_suffix(".json").unlink(missing_ok=True)
            stream_limit = max(1, self.preview_chars * 2)
            fallback = _render(
                None,
                _read_bounded_stream(stdout_buffer, stream_limit),
                _read_bounded_stream(stderr_buffer, stream_limit),
                status,
            )
            notice = (
                f"\n[full-output storage failed: {type(exc).__name__}; "
                "bounded preview only]\n"
            )
            preview = _bounded_text(fallback, self.preview_chars, notice)
            return preview, None, len(fallback)

    def run(
        self,
        cmd: str,
        timeout: float,
        cancel_event: Event | None = None,
    ) -> CommandExecution:
        started = time.monotonic()
        try:
            with (
                tempfile.TemporaryFile(mode="w+b") as stdout_buffer,
                tempfile.TemporaryFile(mode="w+b") as stderr_buffer,
            ):
                process = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=self.workspace,
                    env=_child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_buffer,
                    stderr=stderr_buffer,
                    **_popen_group_options(),
                )
                timed_out = False
                cancelled = False
                termination_error = None
                deadline = started + timeout
                while process.poll() is None:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        termination_error = _terminate_process_tree(process)
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        termination_error = _terminate_process_tree(process)
                        break
                    try:
                        process.wait(timeout=min(0.1, remaining))
                    except subprocess.TimeoutExpired:
                        continue

                if cancelled:
                    status = "Command cancelled by user."
                    if termination_error:
                        status += f" Cleanup warning: {termination_error}."
                    rc = None
                elif timed_out:
                    status = f"Command timed out after {timeout:g} seconds."
                    if termination_error:
                        status += f" Cleanup warning: {termination_error}."
                    rc = None
                else:
                    status = f"exit code: {process.returncode}"
                    rc = process.returncode
                preview, output_ref, output_chars = self._materialize(
                    status,
                    stdout_buffer,
                    stderr_buffer,
                )
                return CommandExecution(
                    preview,
                    not timed_out and not cancelled,
                    rc=rc,
                    output_ref=output_ref,
                    output_chars=output_chars,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    cancelled=cancelled,
                )
        except OSError as exc:
            full = _redact(f"Command runtime error: {type(exc).__name__}: {exc}")
            return CommandExecution(
                _bounded_text(full, self.preview_chars, "\n[output truncated]\n"),
                False,
                output_chars=len(full),
                elapsed_seconds=round(time.monotonic() - started, 3),
            )


def _saved_output_path(
    workspace: str | Path,
    session_id: str | None,
    output_id: str,
) -> Path:
    if not isinstance(output_id, str) or OUTPUT_ID_RE.fullmatch(output_id) is None:
        raise ValueError("output_id must be an id returned by run_command or check_command")
    root = Path(workspace).resolve() / ".agent" / "outputs"
    scopes = [_scope(session_id)]
    if scopes[0] != "runtime":
        scopes.append("runtime")
    for scope in scopes:
        path = root / scope / output_id
        if path.is_file():
            return path
    raise FileNotFoundError(f"saved command output not found: {output_id}")


def _saved_output_chars(path: Path) -> int:
    metadata = path.with_suffix(".json")
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
        chars = value.get("chars")
        if isinstance(chars, int) and chars >= 0:
            return chars
    except (OSError, ValueError, AttributeError):
        pass
    total = 0
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        while chunk := stream.read(STREAM_CHUNK_BYTES):
            total += len(chunk)
    return total


def read_saved_output_range(
    workspace: str | Path,
    session_id: str | None,
    output_id: str,
    offset: int,
    limit: int,
) -> tuple[str, int]:
    """Read one bounded character range without loading the saved result in memory."""

    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    path = _saved_output_path(workspace, session_id, output_id)
    total = _saved_output_chars(path)
    if offset > total:
        raise ValueError(f"offset {offset} is past the end of the output ({total} chars)")
    remaining = offset
    chunks: list[str] = []
    wanted = limit
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        while remaining:
            skipped = stream.read(min(STREAM_CHUNK_BYTES, remaining))
            if not skipped:
                break
            remaining -= len(skipped)
        while wanted:
            chunk = stream.read(min(STREAM_CHUNK_BYTES, wanted))
            if not chunk:
                break
            chunks.append(chunk)
            wanted -= len(chunk)
    return "".join(chunks), total
