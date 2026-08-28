"""Nine local tools: schemas, implementations, registry, and dispatcher."""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path

import config
import ui
from command_runtime import CommandRunner, read_saved_output
from permissions import Decision
from state import State, ToolRes

NOISE_DIRS = {".git", ".agent", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
MAX_READ_LINES = 200
MAX_SEARCH_RESULTS = 30
MAX_GLOB_RESULTS = 100
MAX_SEARCH_FILE_BYTES = 2_000_000
MAX_REPO_MAP_FILES = 200
MAX_REPO_MAP_SYMBOLS = 500


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


def _read_text_data(path: Path) -> tuple[str, bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"file not found: {_relative(path)}")
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"binary file is not supported: {_relative(path)}")
    return data.decode("utf-8", errors="replace"), data


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
        if st.observe_read(f"{_relative(path)}:0:0", digest):
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
    end = min(end, start + MAX_READ_LINES - 1, total)

    shown = [f"{number} | {lines[number - 1]}" for number in range(start, end + 1)]
    header = f"{_relative(path)} {start}-{end} / {total}"
    footer = f"{max(0, start - 1)} lines above, {max(0, total - end)} lines below."
    if capped:
        footer += f" Requested range capped at {MAX_READ_LINES} lines."
    payload = _clip(
        f"{header}\n\n" + "\n".join(shown) + f"\n\n{footer}{outside_change}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    if st.observe_read(f"{_relative(path)}:{start}:{end}", digest):
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
    content = read_saved_output(config.WORKSPACE_DIR, st.session_id, output_id)
    offset = int(args.get("offset", 0))
    requested = int(args.get("limit", min(8_000, config.MAX_TOOL_CHARS)))
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if requested < 1:
        raise ValueError("limit must be >= 1")
    if offset > len(content):
        raise ValueError(f"offset {offset} is past the end of the output ({len(content)} chars)")
    limit = min(requested, config.MAX_TOOL_CHARS)
    requested_end = min(len(content), offset + limit)
    end = requested_end
    for _ in range(2):
        header = f"saved command output {output_id}: chars {offset}-{end} / {len(content)}"
        footer = (
            f"\n\nnext offset: {end}; {len(content) - end} chars remain."
            if end < len(content)
            else "\n\n(end of saved output)"
        )
        overhead = len(header) + 2 + len(footer)
        end = min(requested_end, offset + max(0, config.MAX_TOOL_CHARS - overhead))
    header = f"saved command output {output_id}: chars {offset}-{end} / {len(content)}"
    footer = (
        f"\n\nnext offset: {end}; {len(content) - end} chars remain."
        if end < len(content)
        else "\n\n(end of saved output)"
    )
    payload = f"{header}\n\n{content[offset:end]}{footer}"
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
    print(f"\nProposed change: {rel}")
    ui.show_diff(diff)
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
    new_bytes = new.encode("utf-8")
    if st.checkpoints is not None:
        prepared = st.checkpoints.prepare(
            rel,
            current_data,
            new_bytes,
            st.rev,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write exact UTF-8 bytes so Windows newline translation cannot turn an
    # existing CRLF into CRCRLF or make a no-op look like a content change.
    path.write_bytes(new_bytes)
    st.note_agent_edit(rel)
    st.workspace_tracker.accept(path, new_bytes)
    checkpoint_text = ""
    if prepared is not None:
        st.checkpoints.commit(prepared, st.rev)
        checkpoint_text = f" Checkpoint: {prepared.checkpoint_id}."
    return ToolRes(
        f"Updated {rel}; workspace revision is now {st.rev}.{checkpoint_text}",
        changed_files=[rel],
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
            resolved = _safe_search_candidate(path)
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


def repo_map(args: dict, st: State) -> ToolRes:
    """Build an on-demand Python symbol overview using the standard AST."""

    root = _resolve_safe_path(args.get("path") or ".")
    python_files: list[Path] = []
    for path in _search_files(root):
        try:
            resolved = _safe_search_candidate(path)
        except (OSError, ValueError):
            continue
        if resolved.suffix.lower() == ".py":
            python_files.append(resolved)
    total_files = len(python_files)
    files = python_files[:MAX_REPO_MAP_FILES]
    lines: list[str] = []
    symbols = 0
    parse_errors = 0
    for path in files:
        try:
            text = _read_text(path)
            tree = ast.parse(text, filename=_relative(path))
        except (OSError, SyntaxError, UnicodeError, ValueError):
            parse_errors += 1
            continue

        file_lines: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if symbols >= MAX_REPO_MAP_SYMBOLS:
                    break
                file_lines.append(_symbol_line(node))
                symbols += 1
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if symbols >= MAX_REPO_MAP_SYMBOLS:
                            break
                        file_lines.append(_symbol_line(child, indent="    "))
                        symbols += 1
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if symbols >= MAX_REPO_MAP_SYMBOLS:
                    break
                file_lines.append(_symbol_line(node))
                symbols += 1
        if file_lines:
            lines.append(f"{_relative(path)}\n" + "\n".join(file_lines))
        if symbols >= MAX_REPO_MAP_SYMBOLS:
            break

    header = f"Python repo map: {symbols} symbols from {len(files)} / {total_files} files."
    notes = []
    if total_files > MAX_REPO_MAP_FILES:
        notes.append(f"file scan capped at {MAX_REPO_MAP_FILES}")
    if symbols >= MAX_REPO_MAP_SYMBOLS:
        notes.append(f"symbol output capped at {MAX_REPO_MAP_SYMBOLS}")
    if parse_errors:
        notes.append(f"{parse_errors} files skipped because they could not be parsed")
    if notes:
        header += " " + "; ".join(notes) + "."
    body = "\n\n".join(lines) if lines else "No Python class or function symbols found."
    return ToolRes(_clip(header + "\n\n" + body))


def _timeout(args: dict) -> float:
    requested = float(args.get("timeout", config.CMD_TIMEOUT))
    if requested <= 0:
        raise ValueError("timeout must be > 0")
    return min(requested, config.CMD_TIMEOUT)


def _exec(cmd: str, timeout: float, session_id: str | None = None) -> ToolRes:
    execution = CommandRunner(
        config.WORKSPACE_DIR,
        session_id,
        config.MAX_TOOL_CHARS,
    ).run(cmd, timeout)
    return ToolRes(
        execution.text,
        ok=execution.ok,
        rc=execution.rc,
        output_ref=execution.output_ref,
        output_chars=execution.output_chars,
    )


def _tracked_exec(cmd: str, timeout: float, st: State) -> tuple[ToolRes, bool]:
    """Execute one command and reconcile observed workspace transitions."""

    before, prior_delta = st.workspace_tracker.before_command(_root())
    result = _exec(cmd, timeout, st.session_id)
    _, command_delta = st.workspace_tracker.after_command(before)
    changed = sorted(set(prior_delta.paths) | set(command_delta.paths))
    scan_complete = prior_delta.complete and command_delta.complete
    if changed:
        st.note_workspace_changes(changed)
    st.note_shell_attempt(scan_complete)
    result.changed_files = changed
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
    print(f"\nRun command:\n{cmd}")
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
                st.mark_verified()
                result.text += f"\nVerified workspace revision {st.rev}."
    return result


def run_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=False)


def check_command(args: dict, st: State) -> ToolRes:
    return _command(args, st, verify=True)


ToolFn = Callable[[dict, State], ToolRes]
REG: dict[str, ToolFn] = {
    "read_file": read_file,
    "read_command_output": read_command_output,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "glob_files": glob_files,
    "repo_map": repo_map,
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
        _ensure_workspace_tracking(st)
        return REG[name](args, st)
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
    _schema("read_file", "Read up to 200 numbered lines from a workspace text file. An unchanged repeated range returns a compact reuse notice while its earlier content remains in context.", {
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
    _schema("list_dir", "List one directory level while skipping common generated directories.", {"path": PATH}, []),
    _schema("glob_files", "Find workspace files by a relative glob pattern; returns at most 100 paths.", {
        "pattern": {"type": "string", "description": "Relative glob such as **/*.py or tests/test_*.py."},
        "path": PATH,
    }, ["pattern"]),
    _schema("repo_map", "Summarize Python classes, functions, methods, and line numbers using the local AST.", {
        "path": PATH,
    }, []),
    _schema("search_text", "Find literal or regex matches in workspace text files; returns at most 30 lines.", {
        "query": {"type": "string", "description": "Case-sensitive literal text, or a Python regular expression when regex=true."},
        "path": PATH,
        "regex": {"type": "boolean", "description": "Interpret query as a Python regular expression. Defaults to false."},
    }, ["query"]),
    _schema("run_command", "Run an exploratory shell command locally after user approval.", CMD_PROPS, ["cmd"]),
    _schema("check_command", "Run a focused check or final verifier. If a final verifier is configured, only that exact successful command verifies the current workspace revision.", CMD_PROPS, ["cmd"]),
]

# Compatibility name for code that only needs to inspect the registry.
TOOL_FUNCTIONS = REG
