"""Local tool schemas, implementations, registry, and dispatcher."""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config, ui
from .changes import FileChange, summarize_text_change
from .command_runtime import CommandRunner, read_saved_output_range
from .permissions import Decision
from .planner import PLAN_INVESTIGATION_TOOLS, plan_policy_issue
from .state import State, ToolRes
from .verification import PROGRESS_NO_PROGRESS, PROGRESS_WARNING, verifier_scope

NOISE_DIRS = {".git", ".agent", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
MAX_READ_LINES = 200
MAX_OBSERVATION_LINE_CHARS = 2_000
MAX_LIST_RESULTS = 100
MAX_SEARCH_RESULTS = 30
MAX_GLOB_RESULTS = 100
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_REPO_MAP_FILES = 200
MAX_REPO_MAP_SYMBOLS = 500
MAX_REPO_MAP_FILE_BYTES = 400_000
MAX_REPO_MAP_SYMBOLS_PER_FILE = 30
UTF8_BOM = b"\xef\xbb\xbf"

SOURCE_LANGUAGES = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".rb": "Ruby",
    ".sh": "Shell",
}

DECLARATION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Python": (
        re.compile(r"^\s*(?:async\s+)?def\s+(\w+)"),
        re.compile(r"^\s*class\s+(\w+)"),
    ),
    "JavaScript": (
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)"),
        re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function\b)"),
    ),
    "Go": (
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)"),
        re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface|func)"),
    ),
    "Rust": (
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
        re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|impl)\s+(\w+)"),
    ),
    "Java": (
        re.compile(
            r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*"
            r"(?:class|interface|enum|record)\s+(\w+)"
        ),
    ),
    "C": (re.compile(r"^\s*(?:\w[\w\s*]*?)\b(\w+)\s*\([^;]*\)\s*\{"),),
    "Ruby": (
        re.compile(r"^\s*def\s+([\w.?!]+)"),
        re.compile(r"^\s*(?:class|module)\s+(\w+)"),
    ),
    "Shell": (re.compile(r"^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{"),),
}
DECLARATION_PATTERNS["TypeScript"] = DECLARATION_PATTERNS["JavaScript"] + (
    re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+(\w+)"),
)
DECLARATION_PATTERNS["C++"] = DECLARATION_PATTERNS["C"] + (
    re.compile(r"^\s*(?:class|struct|namespace)\s+(\w+)"),
)

REPO_ENTRY_POINTS = {
    "main.py",
    "__main__.py",
    "app.py",
    "cli.py",
    "server.py",
    "index.js",
    "index.ts",
    "main.go",
    "main.rs",
    "lib.rs",
    "Main.java",
}


def _root() -> Path:
    return Path(config.WORKSPACE_DIR).resolve()


def _resolve_safe_path(relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path must be a non-empty string")
    root = _root()
    target = (root / relative_path).resolve(strict=False)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {relative_path!r}") from exc
    if relative.parts and relative.parts[0] == ".agent":
        raise PermissionError("the private .agent runtime directory is not available to tools")
    return target


def _relative(path: Path) -> str:
    return path.relative_to(_root()).as_posix() or "."


def _clip(text: str) -> str:
    limit = config.MAX_TOOL_CHARS
    if len(text) <= limit:
        return text
    marker = f"\n[output truncated: {len(text)} chars total]\n"
    if limit <= len(marker):
        return marker[:limit]
    room = max(0, limit - len(marker))
    head = room // 2
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _truncate_observation_line(text: str) -> str:
    if len(text) <= MAX_OBSERVATION_LINE_CHARS:
        return text
    marker = f" ... [line truncated: {len(text)} chars] ... "
    room = max(0, MAX_OBSERVATION_LINE_CHARS - len(marker))
    head = (room * 3) // 4
    tail = room - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _page_options(args: dict, maximum: int) -> tuple[int, int, bool, bool]:
    requested = "offset" in args or "limit" in args
    offset = args.get("offset", 0)
    limit = args.get("limit", maximum)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    capped = limit > maximum
    return offset, min(limit, maximum), capped, requested


def _page_text(
    candidates: list[str],
    *,
    total: int,
    offset: int,
    noun: str,
    limit_capped: bool,
    explicitly_requested: bool,
) -> str:
    shown: list[str] = []
    output_capped = False
    for item in candidates:
        proposed = [*shown, item]
        next_offset = offset + len(proposed)
        footer = f"Found {total} {noun}; showing {offset + 1}-{next_offset}."
        if next_offset < total:
            footer += f" next offset: {next_offset}."
        payload = "\n".join(proposed) + "\n\n" + footer
        if len(payload) > config.MAX_TOOL_CHARS and shown:
            output_capped = True
            break
        shown = proposed

    next_offset = offset + len(shown)
    has_more = next_offset < total
    if not explicitly_requested and offset == 0 and not has_more and not limit_capped:
        return "\n".join(shown)

    if offset == 0:
        footer = f"Found {total} {noun}; showing first {next_offset}."
    else:
        footer = f"Found {total} {noun}; showing {offset + 1}-{next_offset}."
    if has_more:
        footer += f" next offset: {next_offset}."
    if limit_capped:
        footer += " Requested limit was capped by the runtime."
    if output_capped:
        footer += " Page ended early to respect the tool-output budget."
    return _clip("\n".join(shown) + "\n\n" + footer)


def _read_text_data(path: Path) -> tuple[str, bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {_relative(path)}")
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"binary file is not supported: {_relative(path)}")
    try:
        text = data[len(UTF8_BOM) :].decode("utf-8") if data.startswith(UTF8_BOM) else data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"text file is not valid UTF-8 and will not be rewritten: {_relative(path)}"
        ) from exc
    return text, data


def _read_text(path: Path) -> str:
    return _read_text_data(path)[0]


def _ensure_workspace_tracking(st: State) -> None:
    root = _root()
    if st.workspace_tracker.root != root or st.workspace_tracker.last_snapshot is None:
        snapshot = st.initialize_workspace_tracking(str(root))
        st.workspace_tracking_complete = (
            st.workspace_tracking_complete and snapshot.complete
        )


def _observe_file(path: Path, data: bytes | None, st: State) -> str:
    """Accept current content and record a newly discovered outside change."""

    if not st.workspace_tracker.observe(path, data):
        return ""
    rel = _relative(path)
    st.note_workspace_changes([rel])
    return (
        f"\n\n[Runtime detected that {rel} changed outside Agent file tools; "
        f"workspace revision is now {st.rev}.]"
    )


def _edit_conflict(path: Path, data: bytes | None, st: State) -> ToolRes | None:
    """Block a stale write until the model reads the newly observed content."""

    reason, newly_seen = st.workspace_tracker.check_edit(path, data)
    if reason is None:
        return None
    rel = _relative(path)
    changed_files: list[str] = []
    if newly_seen:
        st.note_workspace_changes([rel])
        changed_files.append(rel)
    return ToolRes(
        f"Refused stale write to {rel}: {reason}. "
        "Use read_file to observe the current content (or absence) before retrying.",
        blocked=True,
        block_kind="external_change",
        changed_files=changed_files,
    )


def read_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    if not path.is_file():
        _observe_file(path, None, st)
        raise FileNotFoundError(f"file not found: {_relative(path)}")
    text, data = _read_text_data(path)
    outside_change = _observe_file(path, data, st)
    lines = text.splitlines()
    total = len(lines)
    start = int(args.get("start", 1))
    requested_end = args.get("end")
    if start < 1:
        raise ValueError("start must be >= 1")
    if total == 0:
        header = f"{_relative(path)} 0-0 / 0"
        payload = (
            f"{header}\n\n(empty file)\n\n0 lines above, 0 lines below."
            f"{outside_change}"
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if st.observe_read(f"{_relative(path)}:0:0", digest, payload):
            return ToolRes(
                f"{header}\n\n[unchanged: this exact range is already present in recent context.]"
            )
        return ToolRes(payload)
    if total and start > total:
        raise ValueError(f"start {start} is past the end of the file ({total} lines)")
    if requested_end is None:
        end = min(total, start + MAX_READ_LINES - 1)
    else:
        end = int(requested_end)
        if end < start:
            raise ValueError("end must be >= start")
    capped = end - start + 1 > MAX_READ_LINES
    planned_end = min(end, start + MAX_READ_LINES - 1, total)

    shown: list[str] = []
    for number in range(start, planned_end + 1):
        line = _truncate_observation_line(lines[number - 1])
        proposed = [*shown, f"{number} | {line}"]
        proposed_end = start + len(proposed) - 1
        header = f"{_relative(path)} {start}-{proposed_end} / {total}"
        footer = (
            f"{max(0, start - 1)} lines above, "
            f"{max(0, total - proposed_end)} lines below."
        )
        if len(f"{header}\n\n" + "\n".join(proposed) + f"\n\n{footer}") > config.MAX_TOOL_CHARS and shown:
            break
        shown = proposed
    end = start + len(shown) - 1
    header = f"{_relative(path)} {start}-{end} / {total}"
    footer = f"{max(0, start - 1)} lines above, {max(0, total - end)} lines below."
    if capped:
        footer += f" Requested range capped at {MAX_READ_LINES} lines."
    if end < planned_end:
        footer += f" Output budget reached; continue with start={end + 1}."
    payload = _clip(
        f"{header}\n\n" + "\n".join(shown) + f"\n\n{footer}{outside_change}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if st.observe_read(f"{_relative(path)}:{start}:{end}", digest, payload):
        return ToolRes(
            f"{header}\n\n"
            "[unchanged: this exact range is already present in recent context; "
            "reuse that observation instead of retransmitting it.]\n\n"
            f"{footer}"
        )
    return ToolRes(payload)


def read_command_output(args: dict, st: State) -> ToolRes:
    """Read a bounded character range from one saved full command result."""

    output_id = args["output_id"]
    offset = int(args.get("offset", 0))
    requested = int(args.get("limit", min(8_000, config.MAX_TOOL_CHARS)))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if requested < 1:
        raise ValueError("limit must be >= 1")
    limit = min(requested, config.MAX_TOOL_CHARS)
    content, total = read_saved_output_range(
        config.WORKSPACE_DIR,
        st.session_id,
        output_id,
        offset,
        limit,
    )
    end = offset + len(content)
    for _ in range(2):
        header = f"saved command output {output_id}: chars {offset}-{end} / {total}"
        footer = (
            f"\n\nnext offset: {end}; {total - end} chars remain."
            if end < total
            else "\n\n(end of saved output)"
        )
        overhead = len(header) + 2 + len(footer)
        available = max(0, config.MAX_TOOL_CHARS - overhead)
        if len(content) <= available:
            break
        content = content[:available]
        end = offset + len(content)
    header = f"saved command output {output_id}: chars {offset}-{end} / {total}"
    footer = (
        f"\n\nnext offset: {end}; {total - end} chars remain."
        if end < total
        else "\n\n(end of saved output)"
    )
    payload = f"{header}\n\n{content}{footer}"
    return ToolRes(payload[: config.MAX_TOOL_CHARS])


def _diff(path: str, old: str, new: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    return "\n".join(lines) + ("\n" if lines else "")


def _encode_text(text: str, previous: bytes | None) -> bytes:
    data = text.encode("utf-8")
    return UTF8_BOM + data if previous is not None and previous.startswith(UTF8_BOM) else data


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace one file atomically while retaining its permission bits."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.agent-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_file():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_write(
    path: Path,
    old: str,
    new: str,
    expected_data: bytes | None,
    st: State,
) -> ToolRes:
    rel = _relative(path)
    if old == new:
        return ToolRes(f"No change: {rel}")
    diff = _diff(rel, old, new)
    preview_decision = st.permissions.decide_edit(
        rel,
        initially_dirty=st.git_guard.is_initially_dirty(path),
    )
    if preview_decision.decision is Decision.ASK:
        ui.proposed_diff(rel, diff)
    permission = st.permissions.authorize_edit(
        rel,
        initially_dirty=st.git_guard.is_initially_dirty(path),
    )
    if permission.decision is Decision.DENY:
        if permission.user_rejected:
            return ToolRes(f"User rejected changes to {rel}; file was not modified.", rejected=True)
        return ToolRes(
            f"Permission policy blocked changes to {rel}: {permission.reason}",
            blocked=True,
            block_kind="permission",
        )
    current_data = path.read_bytes() if path.is_file() else None
    if current_data != expected_data:
        conflict = _edit_conflict(path, current_data, st)
        if conflict is not None:
            conflict.text = (
                f"{conflict.text} The file changed after the proposed diff was prepared, "
                "so no bytes were written."
            )
            return conflict
        return ToolRes(
            f"Refused write to {rel}: filesystem state changed after the diff was prepared.",
            blocked=True,
            block_kind="external_change",
        )
    prepared = None
    new_bytes = _encode_text(new, expected_data)
    if st.checkpoints is not None:
        prepared = st.checkpoints.prepare(
            rel,
            current_data,
            new_bytes,
            st.rev,
        )
    # A same-directory temporary file plus os.replace leaves readers with
    # either the complete old file or the complete new file after a failure.
    _atomic_write_bytes(path, new_bytes)
    st.note_agent_edit(rel)
    st.workspace_tracker.accept(path, new_bytes)
    checkpoint_text = ""
    if prepared is not None:
        st.checkpoints.commit(prepared, st.rev)
        checkpoint_text = f" Checkpoint: {prepared.checkpoint_id}."
    return ToolRes(
        f"Updated {rel}; workspace revision is now {st.rev}.{checkpoint_text}",
        changed_files=[rel],
        file_changes=[
            summarize_text_change(rel, None if expected_data is None else old, new)
        ],
    )


def write_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    if path.exists() and not path.is_file():
        raise IsADirectoryError(f"not a file: {_relative(path)}")
    new = args["content"]
    if not isinstance(new, str):
        raise TypeError("content must be a string")
    if path.exists():
        old, old_data = _read_text_data(path)
    else:
        old, old_data = "", None
    conflict = _edit_conflict(path, old_data, st)
    if conflict is not None:
        return conflict
    if path.exists() and old.replace("\r\n", "\n").replace("\r", "\n") == new.replace("\r\n", "\n").replace("\r", "\n"):
        return ToolRes(f"No change: {_relative(path)}")
    return _apply_write(path, old, new, old_data, st)


def edit_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    old_fragment = args["old"]
    new_fragment = args["new"]
    if not isinstance(old_fragment, str) or not old_fragment:
        raise ValueError("old must be a non-empty string")
    if not isinstance(new_fragment, str):
        raise TypeError("new must be a string")
    if not path.is_file():
        conflict = _edit_conflict(path, None, st)
        if conflict is not None:
            return conflict
        raise FileNotFoundError(f"file not found: {_relative(path)}")
    current, current_data = _read_text_data(path)
    conflict = _edit_conflict(path, current_data, st)
    if conflict is not None:
        return conflict
    count = current.count(old_fragment)
    if count == 0 and "\r\n" in current and "\r" not in old_fragment:
        native_old = old_fragment.replace("\n", "\r\n")
        native_new = new_fragment.replace("\n", "\r\n")
        native_count = current.count(native_old)
        if native_count:
            old_fragment, new_fragment, count = native_old, native_new, native_count
    if count == 0:
        raise ValueError("old text was not found; read the file again before editing")
    if count > 1:
        raise ValueError(
            f"old text matched {count} times; include more surrounding context so it is unique"
        )
    proposed = current.replace(old_fragment, new_fragment, 1)
    return _apply_write(path, current, proposed, current_data, st)


def multi_edit(args: dict, st: State) -> ToolRes:
    """Apply 2-20 ordered exact replacements to one file, all or nothing."""

    path = _resolve_safe_path(args["path"])
    edits = args["edits"]
    if not isinstance(edits, list) or not 2 <= len(edits) <= 20:
        raise ValueError("edits must be a list containing 2 to 20 replacements")
    if not path.is_file():
        conflict = _edit_conflict(path, None, st)
        if conflict is not None:
            return conflict
        raise FileNotFoundError(f"file not found: {_relative(path)}")

    current, current_data = _read_text_data(path)
    conflict = _edit_conflict(path, current_data, st)
    if conflict is not None:
        return conflict

    proposed = current
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise TypeError(f"edit {index} must be an object")
        extra = set(edit) - {"old", "new"}
        if extra:
            raise ValueError(f"edit {index} has unsupported fields: {', '.join(sorted(extra))}")
        old_fragment = edit.get("old")
        new_fragment = edit.get("new")
        if not isinstance(old_fragment, str) or not old_fragment:
            raise ValueError(f"edit {index} old must be a non-empty string")
        if not isinstance(new_fragment, str):
            raise TypeError(f"edit {index} new must be a string")
        if old_fragment == new_fragment:
            raise ValueError(f"edit {index} would make no change")

        count = proposed.count(old_fragment)
        if count == 0 and "\r\n" in proposed and "\r" not in old_fragment:
            native_old = old_fragment.replace("\n", "\r\n")
            native_new = new_fragment.replace("\n", "\r\n")
            native_count = proposed.count(native_old)
            if native_count:
                old_fragment, new_fragment, count = native_old, native_new, native_count
        if count == 0:
            raise ValueError(
                f"edit {index} old text was not found; no edits were written"
            )
        if count > 1:
            raise ValueError(
                f"edit {index} old text matched {count} times; include more context; "
                "no edits were written"
            )
        proposed = proposed.replace(old_fragment, new_fragment, 1)

    result = _apply_write(path, current, proposed, current_data, st)
    if result.changed_files:
        result.text = f"Applied {len(edits)} exact edits atomically. {result.text}"
    return result


def list_dir(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args.get("path") or ".")
    if not path.is_dir():
        raise NotADirectoryError(f"directory not found: {_relative(path)}")
    entries = []
    for item in sorted(path.iterdir(), key=lambda value: value.name.lower()):
        if item.name in NOISE_DIRS:
            continue
        entries.append(f"[DIR] {item.name}" if item.is_dir() else item.name)
    if not entries:
        return ToolRes("(empty directory)")
    offset, limit, capped, requested = _page_options(args, MAX_LIST_RESULTS)
    if offset >= len(entries):
        raise ValueError(f"offset {offset} is past the end of the listing ({len(entries)} entries)")
    candidates = entries[offset : offset + limit]
    return ToolRes(
        _page_text(
            candidates,
            total=len(entries),
            offset=offset,
            noun="entries",
            limit_capped=capped,
            explicitly_requested=requested,
        )
    )


def _search_files(root: Path):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(f"path not found: {_relative(root)}")
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in NOISE_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _safe_search_candidate(path: Path) -> Path:
    """Resolve every recursive candidate and reject boundary/private-data escapes."""

    resolved = path.resolve(strict=False)
    relative = resolved.relative_to(_root())
    if relative.parts and relative.parts[0] == ".agent":
        raise PermissionError("the private .agent runtime directory is not searchable")
    return resolved


def search_text(args: dict, st: State) -> ToolRes:
    query = args["query"]
    if not isinstance(query, str) or not query:
        raise ValueError("query must be a non-empty string")
    regex = args.get("regex", False)
    if not isinstance(regex, bool):
        raise TypeError("regex must be true or false")
    try:
        pattern = re.compile(query) if regex else None
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    root = _resolve_safe_path(args.get("path") or ".")
    offset, limit, capped, requested = _page_options(args, MAX_SEARCH_RESULTS)
    shown: list[str] = []
    count = 0
    for path in _search_files(root):
        try:
            resolved = _safe_search_candidate(path)
            if resolved.stat().st_size > MAX_SEARCH_FILE_BYTES:
                continue
            data = resolved.read_bytes()
            if b"\x00" in data[:4096]:
                continue
            text = data.decode("utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if (pattern.search(line) if pattern else query in line):
                match_index = count
                count += 1
                if match_index >= offset and len(shown) < limit:
                    shown.append(
                        _truncate_observation_line(
                            f"{_relative(path)}:{lineno}: {line.rstrip()}"
                        )
                    )
    if not count:
        return ToolRes("No matches found.")
    if offset >= count:
        raise ValueError(f"offset {offset} is past the end of the results ({count} matches)")
    return ToolRes(
        _page_text(
            shown,
            total=count,
            offset=offset,
            noun="matches",
            limit_capped=capped,
            explicitly_requested=requested,
        )
    )


def _glob_match(path: str, pattern: str) -> bool:
    if fnmatch.fnmatchcase(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def glob_files(args: dict, st: State) -> ToolRes:
    pattern = args["pattern"]
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    pattern = pattern.replace("\\", "/")
    pattern_path = Path(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        raise PermissionError("glob pattern must stay relative to its search root")
    root = _resolve_safe_path(args.get("path") or ".")
    if not root.exists():
        raise FileNotFoundError(f"path not found: {_relative(root)}")

    offset, limit, capped, requested = _page_options(args, MAX_GLOB_RESULTS)
    matches: list[str] = []
    count = 0
    for path in _search_files(root):
        try:
            resolved = _safe_search_candidate(path)
            relative_to_search = resolved.relative_to(root).as_posix() if root.is_dir() else resolved.name
        except (OSError, ValueError):
            continue
        if _glob_match(relative_to_search, pattern):
            match_index = count
            count += 1
            if match_index >= offset and len(matches) < limit:
                matches.append(_relative(resolved))
    if not count:
        return ToolRes("No files matched.")
    if offset >= count:
        raise ValueError(f"offset {offset} is past the end of the results ({count} files)")
    return ToolRes(
        _page_text(
            matches,
            total=count,
            offset=offset,
            noun="files",
            limit_capped=capped,
            explicitly_requested=requested,
        )
    )


def _argument_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = [argument.arg for argument in [*node.args.posonlyargs, *node.args.args]]
    if node.args.vararg:
        args.append("*" + node.args.vararg.arg)
    elif node.args.kwonlyargs:
        args.append("*")
    args.extend(argument.arg for argument in node.args.kwonlyargs)
    if node.args.kwarg:
        args.append("**" + node.args.kwarg.arg)
    return ", ".join(args)


def _symbol_line(node: ast.AST, indent: str = "  ") -> str:
    if isinstance(node, ast.ClassDef):
        bases = []
        for base in node.bases[:3]:
            bases.append(ast.unparse(base))
        suffix = f"({', '.join(bases)})" if bases else ""
        return f"{indent}L{node.lineno} class {node.name}{suffix}"
    async_prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{indent}L{node.lineno} {async_prefix}def {node.name}({_argument_names(node)})"


@dataclass(frozen=True)
class _RepoOutline:
    path: str
    language: str
    symbols: tuple[str, ...]
    hidden_symbols: int
    score: float

    def render(self, symbol_limit: int) -> tuple[str, int]:
        shown = self.symbols[:symbol_limit]
        lines = [f"{self.path} [{self.language}]", *shown]
        hidden = self.hidden_symbols + len(self.symbols) - len(shown)
        if hidden:
            lines.append(f"  ... {hidden} more declarations")
        return "\n".join(lines), len(shown)


def _regex_symbol_lines(text: str, language: str) -> list[str]:
    patterns = DECLARATION_PATTERNS[language]
    symbols: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        for pattern in patterns:
            match = pattern.match(line)
            if match is None:
                continue
            signature = line.strip().rstrip("{").rstrip()
            key = (match.group(1), signature)
            if key not in seen:
                seen.add(key)
                symbols.append(f"  L{line_number} {signature}")
            break
    return symbols


def _source_symbols(text: str, language: str) -> tuple[list[str], bool]:
    """Return declaration lines and whether Python needed regex fallback."""

    if language != "Python":
        return _regex_symbol_lines(text, language), False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return _regex_symbol_lines(text, language), True

    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(_symbol_line(node))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(_symbol_line(child, indent="    "))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_symbol_line(node))
    return symbols, False


def _repo_score(relative: str, symbol_count: int) -> float:
    depth = relative.count("/")
    score = 10.0 - 1.5 * depth + min(symbol_count, 20) * 0.4
    name = relative.rsplit("/", 1)[-1]
    if name in REPO_ENTRY_POINTS:
        score += 6.0
    lowered = relative.lower()
    if "test" in lowered or "spec" in lowered:
        score -= 5.0
    return score


def repo_map(args: dict, st: State) -> ToolRes:
    """Build a ranked, bounded source outline with exact Python AST symbols."""

    root = _resolve_safe_path(args.get("path") or ".")
    source_files: list[tuple[Path, str]] = []
    for path in _search_files(root):
        try:
            resolved = _safe_search_candidate(path)
        except (OSError, ValueError):
            continue
        language = SOURCE_LANGUAGES.get(resolved.suffix.lower())
        if language:
            source_files.append((resolved, language))

    total_files = len(source_files)
    outlines: list[_RepoOutline] = []
    fallback_files = 0
    oversized_files = 0
    for path, language in source_files[:MAX_REPO_MAP_FILES]:
        try:
            if path.stat().st_size > MAX_REPO_MAP_FILE_BYTES:
                oversized_files += 1
                continue
            text = _read_text(path)
            symbol_lines, used_fallback = _source_symbols(text, language)
        except (OSError, UnicodeError, ValueError):
            continue
        fallback_files += int(used_fallback)
        if not symbol_lines:
            continue
        relative = _relative(path)
        outlines.append(
            _RepoOutline(
                path=relative,
                language=language,
                symbols=tuple(symbol_lines[:MAX_REPO_MAP_SYMBOLS_PER_FILE]),
                hidden_symbols=max(
                    0, len(symbol_lines) - MAX_REPO_MAP_SYMBOLS_PER_FILE
                ),
                score=_repo_score(relative, len(symbol_lines)),
            )
        )

    outlines.sort(key=lambda outline: (-outline.score, outline.path))
    blocks: list[str] = []
    shown_symbols = 0
    omitted_for_budget = 0
    body_budget = max(1_000, config.MAX_TOOL_CHARS - 800)
    for outline in outlines:
        remaining_symbols = MAX_REPO_MAP_SYMBOLS - shown_symbols
        if remaining_symbols <= 0:
            omitted_for_budget += 1
            continue
        block, count = outline.render(remaining_symbols)
        proposed = "\n\n".join([*blocks, block])
        if len(proposed) > body_budget and blocks:
            omitted_for_budget += 1
            continue
        blocks.append(block)
        shown_symbols += count

    languages = sorted({outline.language for outline in outlines})
    language_text = ", ".join(languages) if languages else "none"
    header = (
        f"Repo map: {shown_symbols} symbols from {len(blocks)} / {total_files} "
        f"supported source files. Languages: {language_text}."
    )
    notes: list[str] = []
    if total_files > MAX_REPO_MAP_FILES:
        notes.append(f"file scan capped at {MAX_REPO_MAP_FILES}")
    if fallback_files:
        notes.append(
            f"{fallback_files} Python files could not be parsed; declaration regex fallback used"
        )
    if oversized_files:
        notes.append(f"{oversized_files} oversized source files skipped")
    if omitted_for_budget:
        notes.append(f"{omitted_for_budget} lower-ranked files omitted by output budget")
    if notes:
        header += " " + "; ".join(notes) + "."
    body = "\n\n".join(blocks) if blocks else "No supported source declarations found."
    return ToolRes(_clip(header + "\n\n" + body))


def _timeout(args: dict) -> float:
    requested = float(args.get("timeout", config.CMD_TIMEOUT))
    if requested <= 0:
        raise ValueError("timeout must be > 0")
    return min(requested, config.CMD_TIMEOUT)


def _exec(
    cmd: str,
    timeout: float,
    session_id: str | None = None,
    cancel_event=None,
) -> ToolRes:
    execution = CommandRunner(
        config.WORKSPACE_DIR,
        session_id,
        config.MAX_TOOL_CHARS,
    ).run(cmd, timeout, cancel_event)
    return ToolRes(
        execution.text,
        ok=execution.ok,
        rc=execution.rc,
        output_ref=execution.output_ref,
        output_chars=execution.output_chars,
        elapsed_seconds=execution.elapsed_seconds,
        cancelled=execution.cancelled,
        blocked=execution.cancelled,
        block_kind="user_stopped" if execution.cancelled else None,
    )


def _tracked_exec(cmd: str, timeout: float, st: State) -> tuple[ToolRes, bool]:
    """Execute one command and reconcile observed workspace transitions."""

    before, prior_delta = st.workspace_tracker.before_command(_root())
    result = _exec(cmd, timeout, st.session_id, st.cancel_event)
    after, command_delta = st.workspace_tracker.after_command(before)
    changed = sorted(set(prior_delta.paths) | set(command_delta.paths))
    scan_complete = prior_delta.complete and command_delta.complete
    if changed:
        st.note_workspace_changes(changed)
    st.note_shell_attempt(scan_complete)
    result.changed_files = changed
    result.file_changes = []
    for path in prior_delta.paths:
        if path not in command_delta.paths:
            kind = "deleted" if path not in before.files else "changed"
            result.file_changes.append(FileChange(path, kind, None, None))
    for path in command_delta.paths:
        kind = (
            "added"
            if path not in before.files
            else "deleted"
            if path not in after.files
            else "modified"
        )
        result.file_changes.append(FileChange(path, kind, None, None))
    result.workspace_scan_complete = scan_complete

    notes: list[str] = []
    if prior_delta.paths:
        notes.append(
            "Runtime detected workspace changes made outside Agent file tools before "
            f"this command: {', '.join(prior_delta.paths[:20])}"
        )
    if command_delta.paths:
        notes.append(
            "Command changed workspace files: "
            + ", ".join(command_delta.paths[:20])
            + (" ..." if len(command_delta.paths) > 20 else "")
        )
    if not scan_complete:
        detail = command_delta.note or prior_delta.note or "snapshot limits were reached"
        notes.append(f"Workspace change scan was bounded/partial: {detail}")
    if result.output_ref and notes:
        notes.append(
            f"Full command output: {result.output_ref} ({result.output_chars} chars); "
            "use read_command_output for another range."
        )
    if notes:
        result.text = _clip(result.text + "\n" + "\n".join(notes))
    return result, bool(command_delta.paths)


def run_user_command(
    cmd: str,
    timeout: float | None = None,
    st: State | None = None,
) -> ToolRes:
    """Run a command explicitly entered by the human in interactive shell mode."""

    if not isinstance(cmd, str) or not cmd.strip():
        return ToolRes("User command must not be empty.", ok=False)
    chosen_timeout = config.CMD_TIMEOUT if timeout is None else min(timeout, config.CMD_TIMEOUT)
    if st is None:
        return _exec(cmd, chosen_timeout)
    return _tracked_exec(cmd, chosen_timeout, st)[0]


def _command(args: dict, st: State, verify: bool) -> ToolRes:
    cmd = args["cmd"]
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("cmd must be a non-empty string")
    timeout = _timeout(args)
    permission = st.permissions.authorize_command(
        cmd,
        verification=verify,
        required_verifier=st.required_verifier if verify else None,
    )
    if permission.decision is Decision.DENY:
        if permission.user_rejected:
            return ToolRes(f"User rejected command: {cmd}", rejected=True)
        return ToolRes(
            f"Permission policy blocked command: {permission.reason}",
            blocked=True,
            block_kind="permission",
        )
    result, command_changed_workspace = _tracked_exec(cmd, timeout, st)
    if verify and result.ok and not result.rejected:
        st.last_check_cmd = cmd
        st.last_check_rc = result.rc
        st.last_check_rev = st.rev
        configured_match = bool(
            st.required_verifier
            and cmd.strip() == st.required_verifier.strip()
        )
        scope = verifier_scope(cmd, configured=configured_match)
        if result.rc is not None:
            progress = st.note_check_attempt(cmd, result.text, result.rc, scope)
            if progress == PROGRESS_WARNING:
                result.text += (
                    "\nVerification progress warning: the normalized failure is unchanged "
                    "for a second consecutive check. Re-inspect the root cause before another edit."
                )
            elif progress == PROGRESS_NO_PROGRESS:
                result.text += (
                    "\nNO_PROGRESS: the same normalized verification failure occurred three "
                    "times consecutively. Runtime will stop this task instead of continuing blind edits."
                )
        if result.rc == 0:
            if command_changed_workspace:
                result.text += (
                    "\nCheck changed workspace files, so it did not verify the resulting "
                    "revision. Run check_command again after the workspace is stable."
                )
            elif st.required_verifier and cmd.strip() != st.required_verifier.strip():
                result.text += (
                    "\nCheck passed, but it did not satisfy the configured final verifier. "
                    f"Use check_command with exactly: {st.required_verifier}"
                )
            else:
                st.mark_verified(scope)
                result.text += f"\nVerified workspace revision {st.rev}."
                if st.requires_full_verification and not st.verification_adequate():
                    result.text += (
                        "\nThis successful check is targeted or its scope is unknown; "
                        "the current task explicitly requires a full test-suite verification."
                    )
    return result


def run_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=False)


def check_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=True)


def update_plan(args: dict, st: State) -> ToolRes:
    """Replace the complete navigation plan without changing workspace state."""

    issue = plan_policy_issue(
        args.get("plan"),
        task=st.planner_task,
        existing_workspace=st.planner_existing_workspace,
        investigation_tools=st.planner_observations,
        initial_plan=not st.planner_plan_created_this_turn,
    )
    if issue:
        return ToolRes(f"Plan policy rejected this update: {issue}", ok=False)
    changed = st.plan.replace(
        args.get("plan"),
        explanation=args.get("explanation"),
        preserve_explanation="explanation" not in args,
    )
    st.planner_plan_created_this_turn = True
    prefix = "Plan updated" if changed else "Plan unchanged"
    return ToolRes(
        f"{prefix}: {st.plan.progress_text()}\n{st.plan.compact()}",
        plan_updated=changed,
    )


ToolFn = Callable[[dict, State], ToolRes]
REG: dict[str, ToolFn] = {
    "read_file": read_file,
    "read_command_output": read_command_output,
    "write_file": write_file,
    "edit_file": edit_file,
    "multi_edit": multi_edit,
    "list_dir": list_dir,
    "glob_files": glob_files,
    "repo_map": repo_map,
    "search_text": search_text,
    "update_plan": update_plan,
    "run_command": run_command,
    "check_command": check_command,
}


def run_tool(name: str, args: dict, st: State) -> ToolRes:
    if name not in REG:
        return ToolRes(f"Unknown tool {name!r}. Available tools: {', '.join(REG)}", ok=False)
    if not isinstance(args, dict):
        return ToolRes("Tool arguments must be a JSON object.", ok=False)
    try:
        _ensure_workspace_tracking(st)
        result = REG[name](args, st)
        if result.ok and name in PLAN_INVESTIGATION_TOOLS:
            st.note_planner_observation(name, args)
        return result
    except (KeyError, TypeError, ValueError, PermissionError, OSError) as exc:
        return ToolRes(f"Tool error in {name}: {type(exc).__name__}: {exc}", ok=False)
    except Exception as exc:  # noqa: BLE001 - tool boundary must become an observation
        return ToolRes(f"Unexpected tool error in {name}: {type(exc).__name__}: {exc}", ok=False)


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


PATH = {"type": "string", "description": "Path relative to the workspace root."}
CMD_PROPS = {
    "cmd": {
        "type": "string",
        "description": (
            f"Shell command for platform {os.name}. It already runs in the workspace; "
            "do not prepend cd and do not inspect the private .agent directory."
        ),
    },
    "timeout": {"type": "number", "description": "Optional timeout in seconds, capped by runtime configuration."},
}

TOOL_SCHEMAS = [
    _schema("read_file", "Read up to 200 numbered lines from a workspace text file. The Runtime retains a bounded exact working set, so an unchanged repeated range can return a compact reuse notice even after older conversation groups are pruned.", {
        "path": PATH,
        "start": {"type": "integer", "minimum": 1, "description": "First line, 1-based."},
        "end": {"type": "integer", "minimum": 1, "description": "Optional inclusive final line."},
    }, ["path"]),
    _schema("read_command_output", "Read a bounded character range from a full command output saved by the Runtime after its preview was truncated.", {
        "output_id": {"type": "string", "description": "Opaque id returned by run_command or check_command."},
        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based character offset. Defaults to 0."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum characters to read, capped by runtime configuration."},
    }, ["output_id"]),
    _schema("write_file", "Create or replace a complete text file after showing a diff and requesting approval.", {
        "path": PATH,
        "content": {"type": "string", "description": "Complete new file content."},
    }, ["path", "content"]),
    _schema("edit_file", "Replace exactly one unique text fragment after showing a diff and requesting approval.", {
        "path": PATH,
        "old": {"type": "string", "description": "Non-empty text that must occur exactly once."},
        "new": {"type": "string", "description": "Replacement text."},
    }, ["path", "old", "new"]),
    _schema("multi_edit", "Apply 2-20 related exact replacements to one file as one atomic change. Every edit is dry-run in order before one diff, approval, checkpoint, and write; any failure leaves the file unchanged.", {
        "path": PATH,
        "edits": {
            "type": "array",
            "minItems": 2,
            "maxItems": 20,
            "description": "Ordered replacements; each edit sees the result of earlier edits.",
            "items": {
                "type": "object",
                "properties": {
                    "old": {"type": "string", "description": "Non-empty text that must occur exactly once at this step."},
                    "new": {"type": "string", "description": "Replacement text."},
                },
                "required": ["old", "new"],
                "additionalProperties": False,
            },
        },
    }, ["path", "edits"]),
    _schema("list_dir", "List one directory level with bounded, recoverable pagination.", {
        "path": PATH,
        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based entry offset. Defaults to 0."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum entries for this page, capped at 100."},
    }, []),
    _schema("glob_files", "Find workspace files by a relative glob pattern with pagination.", {
        "pattern": {"type": "string", "description": "Relative glob such as **/*.py or tests/test_*.py."},
        "path": PATH,
        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based result offset. Defaults to 0."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum paths for this page, capped at 100."},
    }, ["pattern"]),
    _schema("repo_map", "Build a ranked source outline with line numbers. Python uses the standard AST; JavaScript/TypeScript, Go, Rust, Java, C/C++, Ruby, and Shell use conservative declaration matching.", {
        "path": PATH,
    }, []),
    _schema("search_text", "Find literal or regex matches with bounded, recoverable pagination.", {
        "query": {"type": "string", "description": "Case-sensitive literal text, or a Python regular expression when regex=true."},
        "path": PATH,
        "regex": {"type": "boolean", "description": "Interpret query as a Python regular expression. Defaults to false."},
        "offset": {"type": "integer", "minimum": 0, "description": "Zero-based match offset. Defaults to 0."},
        "limit": {"type": "integer", "minimum": 1, "description": "Maximum matches for this page, capped at 30."},
    }, ["query"]),
    _schema("update_plan", "Update the complete current plan for non-trivial multi-step work after investigating an existing repository. Use task-specific technical milestones (usually 3-7), not generic implementation/test/README templates. Update statuses as milestones advance and revise text only when evidence changes the route. The plan is navigation only; verification remains authoritative.", {
        "explanation": {"type": "string", "maxLength": 400, "description": "Optional brief reason for the update."},
        "plan": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "step": {"type": "string", "minLength": 1, "maxLength": 160},
                    "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                },
                "required": ["step", "status"],
                "additionalProperties": False,
            },
        },
    }, ["plan"]),
    _schema("run_command", "Run an exploratory shell command locally after user approval.", CMD_PROPS, ["cmd"]),
    _schema("check_command", "Run a focused check or final verifier. If a final verifier is configured, only that exact successful command verifies the current workspace revision. When the user explicitly requires all tests, a targeted test file is intermediate evidence and a recognized repository-wide suite is required before final.", CMD_PROPS, ["cmd"]),
]

# Compatibility name for code that only needs to inspect the registry.
TOOL_FUNCTIONS = REG
