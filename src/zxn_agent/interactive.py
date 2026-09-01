"""Low-cost helpers for the persistent interactive command-line experience."""

from __future__ import annotations

import re
from pathlib import Path

from . import config
from .state import State, ToolRes

FILE_REF_RE = re.compile(r'(?<!\S)@(?:"([^"]+)"|(\S+))')
MAX_FILE_REFERENCES = 5


HELP = """Interactive commands:
  /help                 show this help
  /status               show session, revision, verification, and checkpoints
  /sessions             list recent sessions for this workspace
  /resume [id]          resume the latest or selected local session
  /new or /clear        start a fresh context on the next prompt
  /checkpoints          list restorable Agent file edits
  /undo                 restore the latest Agent file edit if it has no conflict
  /restore <id>         restore a selected active checkpoint
  /exit                 leave the Agent

Input shortcuts:
  @path or @"path with spaces"  attach a bounded text file to the next task
  !command                       run a human-entered shell command, then choose
                                 whether its output is shown to the model
"""


def _safe_reference(workspace: str | Path, relative_path: str) -> Path:
    root = Path(workspace).resolve()
    target = (root / relative_path).resolve(strict=False)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PermissionError("file reference escapes the workspace") from exc
    if relative.parts and relative.parts[0] == ".agent":
        raise PermissionError("private .agent session data cannot be attached")
    if not target.is_file():
        raise FileNotFoundError("referenced file was not found")
    return target


def expand_file_references(text: str, workspace: str | Path) -> tuple[str, list[str]]:
    """Append explicitly referenced text files without adding another model call."""

    matches = list(FILE_REF_RE.finditer(text))
    if not matches:
        return text, []
    references = []
    notes = []
    remaining = config.MAX_FILE_REFERENCE_TOTAL_CHARS
    for match in matches[:MAX_FILE_REFERENCES]:
        raw_path = (match.group(1) or match.group(2) or "").strip()
        if raw_path in references:
            continue
        references.append(raw_path)
        try:
            path = _safe_reference(workspace, raw_path)
            data = path.read_bytes()
            if b"\x00" in data[:4096]:
                raise ValueError("binary files are not supported by @file")
            content = data.decode("utf-8", errors="replace")
            original = len(content)
            limit = min(config.MAX_FILE_REFERENCE_CHARS, remaining)
            if limit <= 0:
                notes.append(
                    f"[Could not attach @{raw_path}: aggregate @file budget exhausted]"
                )
                continue
            content = content[:limit]
            remaining -= len(content)
            if original > limit:
                content += f"\n[truncated from {original} characters]"
            relative = path.relative_to(Path(workspace).resolve()).as_posix()
            notes.append(f'<referenced_file path="{relative}">\n{content}\n</referenced_file>')
        except (OSError, ValueError, PermissionError) as exc:
            notes.append(f"[Could not attach @{raw_path}: {exc}]")
    if len(matches) > MAX_FILE_REFERENCES:
        notes.append(f"[Only the first {MAX_FILE_REFERENCES} @file references were considered.]")
    expanded = text + "\n\nUser-explicit file references:\n" + "\n\n".join(notes)
    return expanded, references


def shell_observation(cmd: str, result: ToolRes) -> str:
    return (
        "[User shell observation]\n"
        f"The user explicitly ran this command in the workspace:\n{cmd}\n\n"
        f"{result.text}"
    )


def status_text(st: State, session_path: Path | None, checkpoint_count: int) -> str:
    session = f"{st.session_id} ({session_path})" if session_path else "none"
    verified = (
        "yes"
        if st.verification_required() and st.ok_rev == st.rev
        else "no"
        if st.verification_required()
        else "not required"
    )
    usage_details: list[str] = []
    if st.task_cache_usage_reported:
        usage_details.append(
            f"cache hit/miss {st.task_cache_hit_tok}/{st.task_cache_miss_tok}"
        )
    if st.task_reasoning_usage_reported:
        usage_details.append(f"reasoning {st.task_reasoning_tok}")
    task_usage = f"current task {st.task_tokens}"
    if usage_details:
        task_usage += f" ({'; '.join(usage_details)})"
    return (
        f"session: {session}\n"
        f"permission mode: {config.PERMISSION_MODE}\n"
        f"revision: {st.rev}; verified current revision: {verified}\n"
        f"tracked changed files: {', '.join(sorted(st.files)) if st.files else 'none'}\n"
        f"workspace tracking complete: {'yes' if st.workspace_tracking_complete else 'no (partial)'}\n"
        f"plan: {st.plan.progress_text()}\n"
        f"active checkpoints: {checkpoint_count}\n"
        f"tokens: session {st.in_tok + st.out_tok}; {task_usage}"
    )
