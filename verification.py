"""Deterministic verification-scope policy for explicit full-suite requests."""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path

FULL_SUITE = "full"
TARGETED = "targeted"
UNKNOWN = "unknown"
CONFIGURED = "configured"

PROGRESS_NOT_CHECKED = "not_checked"
PROGRESS_FAILED = "failed"
PROGRESS_WARNING = "warning"
PROGRESS_NO_PROGRESS = "no_progress"
PROGRESS_PASSED = "passed"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_CLOCK_TIME = re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec(?:onds?)?|minutes?)\b", re.IGNORECASE)
_ADDRESS = re.compile(r"\b0x[0-9a-f]{6,}\b", re.IGNORECASE)
_LONG_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.IGNORECASE)
_FILE_LINE = re.compile(r"(?P<file>[^\s:/\\]+\.[A-Za-z0-9_]+):\d+(?::\d+)?")
_ABSOLUTE_PREFIX = re.compile(
    r"(?:(?:[A-Za-z]:)?[\\/](?:[^\s:\n\\/]+[\\/])+)(?=[^\s:\n\\/]+\.[A-Za-z0-9_]+)",
)
_NOISE_LINE = re.compile(
    r"^(?:=+\s*)?(?:test session starts|platform |cachedir:|rootdir:|configfile:|plugins:|"
    r"collected \d+ items?|warnings summary|short test summary info)(?:\s|$)",
    re.IGNORECASE,
)

_FULL_SUITE_REQUEST = re.compile(
    r"(?:全部|所有|全量)(?:现有|已有|当前)?(?:的)?测试|"
    r"完整(?:的)?测试套件|"
    r"\b(?:all|full|entire)\s+(?:existing\s+|current\s+)?(?:tests?|test\s+suite)\b|"
    r"\bfull[- ]suite\b",
    re.IGNORECASE,
)
_NEGATED_REQUEST = re.compile(
    r"(?:不要|无需|不必|禁止|避免)[^，。；;.!?]{0,16}$|"
    r"(?:do\s+not|don't|avoid|no\s+need\s+to)[^,.;!?]{0,28}$",
    re.IGNORECASE,
)
_SHELL_COMPOSITION = re.compile(r"(?:&&|\|\||[|;&<>`\r\n]|\$\()")


def task_requires_full_suite(task: str) -> bool:
    """Detect only an explicit user request for repository-wide tests."""

    visible = task.split("\n\nUser-explicit file references:", 1)[0]
    for match in _FULL_SUITE_REQUEST.finditer(visible):
        prefix = visible[max(0, match.start() - 32) : match.start()]
        if _NEGATED_REQUEST.search(prefix) is None:
            return True
    return False


def normalize_failure_output(text: str, *, max_chars: int = 24_000) -> str:
    """Remove volatile command noise while preserving failure identity.

    The result is only a repair-progress signal. It is never used to decide
    whether verification passed or whether the final gate may open.
    """

    value = _ANSI_ESCAPE.sub("", str(text)).replace("\r\n", "\n").replace("\r", "\n")
    value = _ISO_TIMESTAMP.sub("<timestamp>", value)
    value = _CLOCK_TIME.sub("<time>", value)
    value = _DURATION.sub("<duration>", value)
    value = _ADDRESS.sub("<address>", value)
    value = _LONG_ID.sub("<id>", value)
    value = _ABSOLUTE_PREFIX.sub("<path>/", value)
    value = _FILE_LINE.sub(lambda match: f"{match.group('file')}:<line>", value)

    lines: list[str] = []
    for raw in value.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or _NOISE_LINE.match(line):
            continue
        # Progress-only rows vary with terminal width and add no failure identity.
        if re.fullmatch(r"[.FsExX%\[\] 0-9]+", line):
            continue
        lines.append(line)
    normalized = "\n".join(lines)
    if len(normalized) > max_chars:
        half = max_chars // 2
        normalized = normalized[:half] + "\n[normalized output bounded]\n" + normalized[-half:]
    return normalized or "<empty failure output>"


def failure_fingerprint(text: str) -> tuple[str, str]:
    """Return a SHA-256 failure identity and its bounded normalized evidence."""

    normalized = normalize_failure_output(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), normalized


def verifier_scope(cmd: str, *, configured: bool = False) -> str:
    """Classify common verifier commands without claiming semantic certainty."""

    if configured:
        return CONFIGURED
    if not isinstance(cmd, str) or not cmd.strip() or _SHELL_COMPOSITION.search(cmd):
        return UNKNOWN
    tokens = _tokens(cmd)
    if not tokens:
        return UNKNOWN
    lowered = [token.lower() for token in tokens]
    executable = _executable(lowered[0])

    pytest_index = _pytest_index(executable, lowered)
    if pytest_index is not None:
        return _pytest_scope(lowered[pytest_index + 1 :])

    unittest_index = _module_index(executable, lowered, "unittest")
    if unittest_index is not None:
        return FULL_SUITE if "discover" in lowered[unittest_index + 1 :] else TARGETED

    if executable in {"npm", "pnpm", "yarn"} and len(lowered) >= 2:
        if lowered[1] == "test" and "--" not in lowered[2:]:
            return FULL_SUITE
        return UNKNOWN
    if executable == "cargo" and len(lowered) >= 2 and lowered[1] == "test":
        trailing = [token for token in lowered[2:] if not token.startswith("-")]
        return FULL_SUITE if not trailing else TARGETED
    if executable == "go" and lowered[1:3] == ["test", "./..."]:
        return FULL_SUITE
    if executable == "dotnet" and len(lowered) >= 2 and lowered[1] == "test":
        return FULL_SUITE
    if executable in {"mvn", "mvnw", "gradle", "gradlew"} and any(
        token in {"test", "check", "verify"} for token in lowered[1:]
    ):
        return FULL_SUITE
    return UNKNOWN


def _tokens(cmd: str) -> list[str]:
    try:
        return [token.strip('"\'') for token in shlex.split(cmd, posix=False)]
    except ValueError:
        return []


def _executable(token: str) -> str:
    name = Path(token).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _module_index(executable: str, tokens: list[str], module: str) -> int | None:
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable) and (
        len(tokens) >= 3 and tokens[1:3] == ["-m", module]
    ):
        return 2
    return None


def _pytest_index(executable: str, tokens: list[str]) -> int | None:
    if executable in {"pytest", "py.test"}:
        return 0
    return _module_index(executable, tokens, "pytest")


def _pytest_scope(args: list[str]) -> str:
    selection_flags = {"-k", "-m", "--lf", "--last-failed", "--ff", "--failed-first"}
    if any(flag in args for flag in selection_flags):
        return TARGETED

    value_options = {
        "--maxfail",
        "--tb",
        "--capture",
        "--color",
        "--durations",
        "--junitxml",
        "--cov",
        "--cov-report",
        "-o",
    }
    positionals: list[str] = []
    skip_value = False
    for token in args:
        if skip_value:
            skip_value = False
            continue
        if token in value_options:
            skip_value = True
            continue
        if token.startswith("-") or (
            "=" in token and token.split("=", 1)[0] in value_options
        ):
            continue
        positionals.append(token.rstrip("/\\"))
    if not positionals:
        return FULL_SUITE
    roots = {".", "tests", "test"}
    return (
        FULL_SUITE
        if all(path.replace("\\", "/") in roots for path in positionals)
        else TARGETED
    )
