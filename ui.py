"""Small ANSI terminal helpers; no TUI and no third-party dependency."""

from __future__ import annotations

import os
import sys


COLORS = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _enabled() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def color(text: str, name: str) -> str:
    if not _enabled():
        return text
    return f"{COLORS[name]}{text}{COLORS['reset']}"


def show_diff(diff: str) -> None:
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            line = color(line, "green")
        elif line.startswith("-") and not line.startswith("---"):
            line = color(line, "red")
        elif line.startswith("@@"):
            line = color(line, "cyan")
        print(line)


def tool(name: str, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    print(f"{color('[tool]', 'cyan')} {color(name, 'bold')}{suffix}")


def success(text: str) -> None:
    print(color(text, "green"))


def warning(text: str) -> None:
    print(color(text, "yellow"))


def error(text: str) -> None:
    print(color(text, "red"))
