"""Eight local tools: schemas, implementations, registry, and dispatcher."""

from __future__ import annotations

import difflib
import fnmatch
import os
from pathlib import Path
import re
import subprocess
from typing import Callable

import config
from permissions import Decision
from state import State, ToolRes
import ui


NOISE_DIRS = {".git", ".agent", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
MAX_READ_LINES = 200
MAX_SEARCH_RESULTS = 30
MAX_GLOB_RESULTS = 100
MAX_SEARCH_FILE_BYTES = 2_000_000


def _root() -> Path:
    return Path(config.WORKSPACE_DIR).resolve()


def _resolve_safe_path(relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("path must be a non-empty string")
    root = _root()
    target = (root / relative_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"path escapes workspace: {relative_path!r}") from exc
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


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {_relative(path)}")
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"binary file is not supported: {_relative(path)}")
    return data.decode("utf-8", errors="replace")


def read_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    text = _read_text(path)
    lines = text.splitlines()
    total = len(lines)
    start = int(args.get("start", 1))
    requested_end = args.get("end")
    if start < 1:
        raise ValueError("start must be >= 1")
    if total == 0:
        return ToolRes(f"{_relative(path)} 0-0 / 0\n\n(empty file)\n\n0 lines above, 0 lines below.")
    if total and start > total:
        raise ValueError(f"start {start} is past the end of the file ({total} lines)")
    if requested_end is None:
        end = min(total, start + MAX_READ_LINES - 1)
    else:
        end = int(requested_end)
        if end < start:
            raise ValueError("end must be >= start")
    capped = end - start + 1 > MAX_READ_LINES
    end = min(end, start + MAX_READ_LINES - 1, total)

    shown = [f"{number} | {lines[number - 1]}" for number in range(start, end + 1)]
    header = f"{_relative(path)} {start}-{end} / {total}"
    footer = f"{max(0, start - 1)} lines above, {max(0, total - end)} lines below."
    if capped:
        footer += f" Requested range capped at {MAX_READ_LINES} lines."
    return ToolRes(_clip(f"{header}\n\n" + "\n".join(shown) + f"\n\n{footer}"))


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


def _apply_write(path: Path, old: str, new: str, st: State) -> ToolRes:
    rel = _relative(path)
    if old == new:
        return ToolRes(f"No change: {rel}")
    diff = _diff(rel, old, new)
    print(f"\nProposed change: {rel}")
    ui.show_diff(diff)
    permission = st.permissions.authorize_edit(
        rel,
        initially_dirty=st.git_guard.is_initially_dirty(path),
    )
    if permission.decision is Decision.DENY:
        if permission.user_rejected:
            return ToolRes(f"User rejected changes to {rel}; file was not modified.", rejected=True)
        return ToolRes(f"Permission policy blocked changes to {rel}: {permission.reason}", blocked=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write exact UTF-8 bytes so Windows newline translation cannot turn an
    # existing CRLF into CRCRLF or make a no-op look like a content change.
    path.write_bytes(new.encode("utf-8"))
    st.rev += 1
    st.changed = True
    st.files.add(rel)
    return ToolRes(f"Updated {rel}; workspace revision is now {st.rev}.")


def write_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    if path.exists() and not path.is_file():
        raise IsADirectoryError(f"not a file: {_relative(path)}")
    new = args["content"]
    if not isinstance(new, str):
        raise ValueError("content must be a string")
    old = _read_text(path) if path.exists() else ""
    if path.exists() and old.replace("\r\n", "\n").replace("\r", "\n") == new.replace("\r\n", "\n").replace("\r", "\n"):
        return ToolRes(f"No change: {_relative(path)}")
    return _apply_write(path, old, new, st)


def edit_file(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args["path"])
    old_fragment = args["old"]
    new_fragment = args["new"]
    if not isinstance(old_fragment, str) or not old_fragment:
        raise ValueError("old must be a non-empty string")
    if not isinstance(new_fragment, str):
        raise ValueError("new must be a string")
    current = _read_text(path)
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
    return _apply_write(path, current, proposed, st)


def list_dir(args: dict, st: State) -> ToolRes:
    path = _resolve_safe_path(args.get("path") or ".")
    if not path.is_dir():
        raise NotADirectoryError(f"directory not found: {_relative(path)}")
    entries = []
    for item in sorted(path.iterdir(), key=lambda value: value.name.lower()):
        if item.name in NOISE_DIRS:
            continue
        entries.append(f"[DIR] {item.name}" if item.is_dir() else item.name)
    return ToolRes(_clip("\n".join(entries) if entries else "(empty directory)"))


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


def search_text(args: dict, st: State) -> ToolRes:
    query = args["query"]
    if not isinstance(query, str) or not query:
        raise ValueError("query must be a non-empty string")
    regex = args.get("regex", False)
    if not isinstance(regex, bool):
        raise ValueError("regex must be true or false")
    try:
        pattern = re.compile(query) if regex else None
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc
    root = _resolve_safe_path(args.get("path") or ".")
    shown: list[str] = []
    count = 0
    for path in _search_files(root):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(_root())
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
                count += 1
                if len(shown) < MAX_SEARCH_RESULTS:
                    shown.append(f"{_relative(path)}:{lineno}: {line.rstrip()}")
    if not count:
        return ToolRes("No matches found.")
    suffix = ""
    if count > MAX_SEARCH_RESULTS:
        suffix = f"\n\nFound {count} matches; showing first {MAX_SEARCH_RESULTS}. Narrow the query."
    return ToolRes(_clip("\n".join(shown) + suffix))


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

    matches: list[str] = []
    count = 0
    for path in _search_files(root):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(_root())
            relative_to_search = resolved.relative_to(root).as_posix() if root.is_dir() else resolved.name
        except (OSError, ValueError):
            continue
        if _glob_match(relative_to_search, pattern):
            count += 1
            if len(matches) < MAX_GLOB_RESULTS:
                matches.append(_relative(resolved))
    if not count:
        return ToolRes("No files matched.")
    suffix = ""
    if count > MAX_GLOB_RESULTS:
        suffix = f"\n\nFound {count} files; showing first {MAX_GLOB_RESULTS}. Narrow the pattern."
    return ToolRes(_clip("\n".join(matches) + suffix))


def _timeout(args: dict) -> float:
    requested = float(args.get("timeout", config.CMD_TIMEOUT))
    if requested <= 0:
        raise ValueError("timeout must be > 0")
    return min(requested, config.CMD_TIMEOUT)


def _exec(cmd: str, timeout: float) -> ToolRes:
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=config.WORKSPACE_DIR,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return ToolRes(f"Command timed out after {timeout:g} seconds.", ok=False)
    except OSError as exc:
        return ToolRes(f"Command runtime error: {type(exc).__name__}: {exc}", ok=False)

    parts = [f"exit code: {proc.returncode}"]
    if proc.stdout:
        parts.append(f"stdout:\n{proc.stdout.rstrip()}")
    if proc.stderr:
        parts.append(f"stderr:\n{proc.stderr.rstrip()}")
    return ToolRes(_clip("\n".join(parts)), rc=proc.returncode)


def _command(args: dict, st: State, verify: bool) -> ToolRes:
    cmd = args["cmd"]
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("cmd must be a non-empty string")
    timeout = _timeout(args)
    print(f"\nRun command:\n{cmd}")
    permission = st.permissions.authorize_command(cmd)
    if permission.decision is Decision.DENY:
        if permission.user_rejected:
            return ToolRes(f"User rejected command: {cmd}", rejected=True)
        return ToolRes(f"Permission policy blocked command: {permission.reason}", blocked=True)
    result = _exec(cmd, timeout)
    if verify and result.ok and not result.rejected and result.rc == 0:
        st.ok_rev = st.rev
        result.text += f"\nVerified workspace revision {st.rev}."
    return result


def run_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=False)


def check_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=True)


ToolFn = Callable[[dict, State], ToolRes]
REG: dict[str, ToolFn] = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "glob_files": glob_files,
    "search_text": search_text,
    "run_command": run_command,
    "check_command": check_command,
}


def run_tool(name: str, args: dict, st: State) -> ToolRes:
    if name not in REG:
        return ToolRes(f"Unknown tool {name!r}. Available tools: {', '.join(REG)}", ok=False)
    if not isinstance(args, dict):
        return ToolRes("Tool arguments must be a JSON object.", ok=False)
    try:
        return REG[name](args, st)
    except (KeyError, TypeError, ValueError, PermissionError, OSError) as exc:
        return ToolRes(f"Tool error in {name}: {type(exc).__name__}: {exc}", ok=False)
    except Exception as exc:  # runtime boundary: observations must not crash the agent
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
    _schema("read_file", "Read up to 200 numbered lines from a workspace text file.", {
        "path": PATH,
        "start": {"type": "integer", "minimum": 1, "description": "First line, 1-based."},
        "end": {"type": "integer", "minimum": 1, "description": "Optional inclusive final line."},
    }, ["path"]),
    _schema("write_file", "Create or replace a complete text file after showing a diff and requesting approval.", {
        "path": PATH,
        "content": {"type": "string", "description": "Complete new file content."},
    }, ["path", "content"]),
    _schema("edit_file", "Replace exactly one unique text fragment after showing a diff and requesting approval.", {
        "path": PATH,
        "old": {"type": "string", "description": "Non-empty text that must occur exactly once."},
        "new": {"type": "string", "description": "Replacement text."},
    }, ["path", "old", "new"]),
    _schema("list_dir", "List one directory level while skipping common generated directories.", {"path": PATH}, []),
    _schema("glob_files", "Find workspace files by a relative glob pattern; returns at most 100 paths.", {
        "pattern": {"type": "string", "description": "Relative glob such as **/*.py or tests/test_*.py."},
        "path": PATH,
    }, ["pattern"]),
    _schema("search_text", "Find literal or regex matches in workspace text files; returns at most 30 lines.", {
        "query": {"type": "string", "description": "Case-sensitive literal text, or a Python regular expression when regex=true."},
        "path": PATH,
        "regex": {"type": "boolean", "description": "Interpret query as a Python regular expression. Defaults to false."},
    }, ["query"]),
    _schema("run_command", "Run an exploratory shell command locally after user approval.", CMD_PROPS, ["cmd"]),
    _schema("check_command", "Run a user-approved check; exit code 0 verifies the current workspace revision.", CMD_PROPS, ["cmd"]),
]

# Compatibility name for code that only needs to inspect the registry.
TOOL_FUNCTIONS = REG
