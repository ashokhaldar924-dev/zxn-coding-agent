"""Compact, append-only terminal renderer for human-facing runtime facts."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from .changes import FileChange

COLORS = {
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}
MAX_UI_DETAIL_CHARS = 1_200
MAX_UI_DETAIL_LINES = 8
OUTPUT_ENABLED = True


def set_output_enabled(enabled: bool) -> None:
    """Enable terminal rendering; desktop GUI disables only this display layer."""

    global OUTPUT_ENABLED
    OUTPUT_ENABLED = bool(enabled)


def _enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def color(text: str, name: str) -> str:
    if not _enabled():
        return text
    return f"{COLORS[name]}{text}{COLORS['reset']}"


def _glyph(symbol: str, fallback: str) -> str:
    if not _enabled():
        return fallback
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        symbol.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return fallback
    return symbol


def _inline_separator() -> str:
    return _glyph("·", "-") if _enabled() else "-"


def _safe(value: object) -> str:
    text = str(value)
    secret = os.environ.get("AGENT_API_KEY", "")
    return text.replace(secret, "[REDACTED]") if secret else text


def _short(value: object, limit: int = 240) -> str:
    text = " ".join(_safe(value).split())
    marker = _glyph("…", "...") if _enabled() else "..."
    return text if len(text) <= limit else text[: limit - len(marker)] + marker


def user_task(text: str, workspace: str | Path) -> None:
    if not OUTPUT_ENABLED:
        return
    visible = text.split("\n\nUser-explicit file references:", 1)[0].strip()
    print()
    print(
        f"{color('zxn Coding Agent', 'bold')}  "
        f"{color(_short(Path(workspace), 120), 'dim')}"
    )
    print()
    lines = visible.splitlines() or [visible]
    print(f"{color(_glyph('❯', '>'), 'cyan')} {_safe(lines[0])}")
    for line in lines[1:8]:
        print(f"  {_safe(line)}")
    if len(lines) > 8:
        print(color(f"  {_glyph('…', '...') if _enabled() else '...'}", "dim"))


def assistant_progress(text: str) -> None:
    if not OUTPUT_ENABLED:
        return
    if text.strip():
        print(f"{color(_glyph('•', '-'), 'dim')} {_short(text, 500)}")


def proposed_diff(path: str, diff: str) -> None:
    """Show full diff only when an edit actually needs human approval."""

    if not OUTPUT_ENABLED:
        return
    print(f"\n{color('Proposed change', 'yellow')} {color(_safe(path), 'cyan')}")
    show_diff(diff)


def show_diff(diff: str) -> None:
    if not OUTPUT_ENABLED:
        return
    for raw in diff.splitlines():
        line = _safe(raw)
        if line.startswith("+") and not line.startswith("+++"):
            line = color(line, "green")
        elif line.startswith("-") and not line.startswith("---"):
            line = color(line, "red")
        elif line.startswith("@@"):
            line = color(line, "cyan")
        print(line)


def tool_started(name: str, args: dict[str, Any]) -> None:
    if not OUTPUT_ENABLED:
        return
    if name not in {"run_command", "check_command"}:
        return
    cmd = _short(args.get("cmd", ""), 500)
    label = "Verifying with" if name == "check_command" else "Running"
    print(f"{color(_glyph('•', '-'), 'dim')} {label} {color(cmd, 'bold')}")


def tool_finished(
    name: str,
    args: dict[str, Any],
    result: Any,
    *,
    plan_state: Any = None,
) -> None:
    """Render one compact observation without exposing raw tool-call JSON."""

    if not OUTPUT_ENABLED:
        return
    if name == "update_plan":
        if result.ok and result.plan_updated and plan_state is not None:
            render_plan(plan_state)
        elif not result.ok:
            _tool_issue(result)
        return
    if name in {"run_command", "check_command"}:
        _command_result(result)
        for change in result.file_changes:
            render_file_change(change)
        return
    if result.file_changes:
        for change in result.file_changes:
            render_file_change(change)
        return
    if not result.ok or result.rejected or result.blocked:
        _tool_issue(result)
        return

    path = _short(args.get("path") or ".", 200)
    if name == "read_file":
        start = args.get("start")
        end = args.get("end")
        suffix = f":{start}-{end}" if start and end else f":{start}-" if start else ""
        _action(f"Read {color(path + suffix, 'cyan')}")
    elif name == "read_command_output":
        output_id = _short(args.get("output_id", "saved output"), 120)
        _action(f"Read saved command output {color(output_id, 'cyan')}")
    elif name == "search_text":
        query = _short(args.get("query", ""), 160)
        count = _result_count(result.text, "matches")
        suffix = f" {_inline_separator()} {count} matches" if count is not None else ""
        _action(f'Searched "{query}"{suffix}')
    elif name == "repo_map":
        count = _first_number(result.text, r"Repo map:\s*(\d+)\s+symbols")
        suffix = f" {_inline_separator()} {count} symbols" if count is not None else ""
        _action(f"Inspected repository map{suffix}")
    elif name == "glob_files":
        count = _result_count(result.text, "files")
        suffix = f" {_inline_separator()} {count} files" if count is not None else ""
        _action(f"Matched {_short(args.get('pattern', ''), 160)}{suffix}")
    elif name == "list_dir":
        _action(f"Listed {color(path, 'cyan')}")
    elif name in {"write_file", "edit_file", "multi_edit"}:
        _action(f"No change in {color(path, 'cyan')}")
    else:
        _action(f"Completed {name}")


def render_plan(plan_state: Any) -> None:
    if not OUTPUT_ENABLED:
        return
    print()
    print(color("Plan", "bold"))
    symbols = {
        "completed": (_glyph("✓", "[x]"), "green"),
        "in_progress": (_glyph("●", ">"), "blue"),
        "pending": (_glyph("○", "[ ]"), "dim"),
    }
    for item in plan_state.items:
        symbol, style = symbols[item.status]
        print(f"  {color(symbol, style)} {_safe(item.step)}")


def render_file_change(change: FileChange) -> None:
    if not OUTPUT_ENABLED:
        return
    labels = {
        "added": "Added",
        "modified": "Modified",
        "deleted": "Deleted",
        "changed": "Changed",
    }
    print()
    print(
        f"  {color(labels.get(change.kind, 'Changed'), 'bold')} "
        f"{color(_safe(change.path), 'cyan')}"
    )
    stats = _change_stats(change)
    if stats:
        print(f"    {stats}")


def _change_stats(change: FileChange) -> str:
    if change.additions is None or change.deletions is None:
        return color("line counts unavailable", "dim")
    parts = []
    if change.additions:
        parts.append(color(f"+{change.additions}", "green"))
    if change.deletions:
        parts.append(color(f"-{change.deletions}", "red"))
    return "  ".join(parts) or color("no line-count change", "dim")


def _command_result(result: Any) -> None:
    if result.rejected or result.blocked:
        _tool_issue(result)
        return
    if result.ok and result.rc == 0:
        summary = _test_summary(result.text) or "exit 0"
        print(f"  {color(_glyph('✓', 'OK'), 'green')} {_safe(summary)}")
        return
    rc = f"exit {result.rc}" if result.rc is not None else "runtime error"
    print(f"  {color(_glyph('✗', 'X'), 'red')} Command failed ({rc})")
    for line in _important_output(result.text):
        print(f"    {_safe(line)}")


def _tool_issue(result: Any) -> None:
    symbol = (
        _glyph("⚠", "!")
        if result.rejected or result.blocked
        else _glyph("✗", "X")
    )
    style = "yellow" if result.rejected or result.blocked else "red"
    print(f"  {color(symbol, style)} {_short(result.text, 600)}")


def _important_output(text: str) -> list[str]:
    lines = [
        line.strip()
        for line in _safe(text).splitlines()
        if line.strip()
        and line.strip().lower() not in {"stdout:", "stderr:"}
        and not line.lower().startswith("exit code:")
    ]
    pattern = re.compile(
        r"(?:failed|error|assertion|traceback|exception|timed out|\bpassed\b)",
        re.IGNORECASE,
    )
    selected = [line for line in lines if pattern.search(line)] or lines[-MAX_UI_DETAIL_LINES:]
    selected = selected[-MAX_UI_DETAIL_LINES:]
    bounded: list[str] = []
    used = 0
    for line in selected:
        clipped = _short(line, 300)
        if used + len(clipped) > MAX_UI_DETAIL_CHARS:
            break
        bounded.append(clipped)
        used += len(clipped)
    return bounded


def _test_summary(text: str) -> str | None:
    candidates = []
    for line in _safe(text).splitlines():
        if re.search(r"\b\d+\s+(?:passed|tests? passed)\b", line, re.IGNORECASE):
            candidates.append(line.strip().strip("= "))
        elif line.strip() == "OK":
            candidates.append("tests passed")
    return _short(candidates[-1], 240) if candidates else None


def _result_count(text: str, noun: str) -> int | None:
    count = _first_number(text, rf"Found\s+(\d+)\s+{re.escape(noun)}")
    if count is not None:
        return count
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) if lines else None


def _first_number(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _action(text: str) -> None:
    print(f"{color(_glyph('•', '-'), 'dim')} {text}")


def finish(final_text: str, st: Any, changes: list[FileChange]) -> None:
    if not OUTPUT_ENABLED:
        return
    rule = _glyph("─", "-") * 64
    print("\n" + color(rule, "dim") + "\n")
    heading = (
        f"{_glyph('✓', 'OK')} Completed"
        if st.completed
        else f"{_glyph('⚠', '!')} Task stopped"
    )
    style = "green" if st.completed else "yellow"
    print(color(heading, style))
    if final_text.strip():
        print(f"\n  {_safe(final_text.strip())}")

    if changes:
        print(f"\n{color('Changes', 'bold')}")
        labels = {"added": "A", "modified": "M", "deleted": "D"}
        for change in changes:
            stats = _change_stats(change)
            suffix = f"  {stats}" if stats else ""
            print(f"  {labels.get(change.kind, 'M')} {color(_safe(change.path), 'cyan')}{suffix}")

    if not st.completed and st.plan.items:
        print(f"\n{color('Plan at stop', 'bold')}")
        symbols = {
            "completed": (_glyph("✓", "[x]"), "green"),
            "in_progress": (_glyph("●", ">"), "blue"),
            "pending": (_glyph("○", "[ ]"), "dim"),
        }
        for item in st.plan.items:
            symbol, item_style = symbols[item.status]
            print(f"  {color(symbol, item_style)} {_safe(item.step)}")

    if st.verification_current():
        heading = "Verification" if st.completed else "Last Verification"
        print(f"\n{color(heading, 'bold')}")
        if st.last_check_cmd:
            print(f"  {color(_glyph('✓', 'OK'), 'green')} {_short(st.last_check_cmd, 500)}")
        print(
            f"  {color(_glyph('✓', 'OK'), 'green')} "
            f"workspace rev {st.rev} / verified {st.ok_rev}"
        )
        print(f"  {color(_glyph('✓', 'OK'), 'green')} fingerprint matched")
        if st.completed and st.verification_satisfied():
            print(f"\n{color('FINAL VERIFIED', 'green')}")
        elif st.requires_full_verification and not st.verification_adequate():
            print(f"\n{color('PARTIALLY VERIFIED', 'yellow')}")
            print("  Full-suite verification was not completed.")
        else:
            print(f"\n{color('LAST VERIFICATION CURRENT', 'green')}")
            print("  Task did not complete; this is not a final success state.")
    else:
        if st.verification_required():
            print(f"\n{color(_glyph('⚠', '!') + ' Verification stale', 'yellow')}")
            print("  workspace is not covered by a current adequate check")


def success(text: str) -> None:
    if not OUTPUT_ENABLED:
        return
    print(color(_safe(text), "green"))


def warning(text: str) -> None:
    if not OUTPUT_ENABLED:
        return
    print(color(_safe(text), "yellow"))


def error(text: str) -> None:
    if not OUTPUT_ENABLED:
        return
    print(color(_safe(text), "red"))
