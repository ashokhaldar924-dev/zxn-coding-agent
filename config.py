"""Runtime configuration loaded from process or Windows user environment."""

from __future__ import annotations

import os
from pathlib import Path


def _windows_user_environment(name: str) -> str | None:
    """Read a persistent per-user variable without copying it into project files."""

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (ImportError, OSError):
        return None
    return value if isinstance(value, str) else None


def _setting(name: str, default: str | None = None) -> str | None:
    """Prefer this process, then the persistent Windows user environment."""

    value = os.environ.get(name)
    if value is not None:
        return value
    value = _windows_user_environment(name)
    return value if value is not None else default


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = _setting(name, str(default))
    assert raw is not None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = _setting(name, str(default))
    assert raw is not None
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _setting(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false, got {raw!r}")


API_BASE_URL = (_setting("AGENT_BASE_URL", "https://api.openai.com/v1") or "").rstrip("/")
MODEL_NAME = _setting("AGENT_MODEL", "") or ""
WORKSPACE_DIR = str(
    Path(_setting("AGENT_WORKSPACE", os.getcwd()) or os.getcwd()).expanduser().resolve()
)

MAX_STEPS = _int("AGENT_MAX_STEPS", 30)
MAX_TIME = _float("AGENT_MAX_TIME", 600.0)
MAX_TOOL_CHARS = _int("AGENT_MAX_TOOL_CHARS", 12_000)
MAX_GROUPS = _int("AGENT_MAX_GROUPS", 8)
MAX_CONTEXT_CHARS = _int("AGENT_MAX_CONTEXT_CHARS", 60_000)
MAX_CONTEXT_TOKENS = _int("AGENT_MAX_CONTEXT_TOKENS", 32_000)
CONTEXT_OUTPUT_RESERVE_TOKENS = _int(
    "AGENT_CONTEXT_OUTPUT_RESERVE_TOKENS",
    min(4_096, MAX_CONTEXT_TOKENS // 8),
    minimum=0,
)
if CONTEXT_OUTPUT_RESERVE_TOKENS >= MAX_CONTEXT_TOKENS:
    raise RuntimeError(
        "AGENT_CONTEXT_OUTPUT_RESERVE_TOKENS must be smaller than "
        "AGENT_MAX_CONTEXT_TOKENS"
    )
MAX_TASK_TOKENS = _int("AGENT_MAX_TASK_TOKENS", 0, minimum=0)
CONTEXT_KEEP_FULL_GROUPS = _int("AGENT_CONTEXT_KEEP_FULL_GROUPS", 1)
if CONTEXT_KEEP_FULL_GROUPS > MAX_GROUPS:
    raise RuntimeError("AGENT_CONTEXT_KEEP_FULL_GROUPS cannot exceed AGENT_MAX_GROUPS")
MAX_PROJECT_CONTEXT_CHARS = _int("AGENT_MAX_PROJECT_CONTEXT_CHARS", 12_000)
MAX_FILE_REFERENCE_CHARS = _int("AGENT_MAX_FILE_REFERENCE_CHARS", 12_000)
MAX_FILE_REFERENCE_TOTAL_CHARS = _int("AGENT_MAX_FILE_REFERENCE_TOTAL_CHARS", 20_000)
CMD_TIMEOUT = _float("AGENT_CMD_TIMEOUT", 60.0)
MAX_ERRORS = _int("AGENT_MAX_ERRORS", 4)
MAX_IDENTICAL_CALLS = _int("AGENT_MAX_IDENTICAL_CALLS", 3, minimum=2)
REQUIRE_CONFIRMATION = _bool("AGENT_CONFIRM", True)
PERMISSION_MODE = (_setting("AGENT_PERMISSION_MODE", "balanced") or "").strip().lower()
if PERMISSION_MODE not in {"balanced", "manual"}:
    raise RuntimeError("AGENT_PERMISSION_MODE must be balanced or manual")


def get_api_key() -> str:
    key = (_setting("AGENT_API_KEY", "") or "").strip()
    if not key:
        raise RuntimeError(
            "Missing AGENT_API_KEY. Set it in the process or Windows user environment."
        )
    return key


def get_model() -> str:
    if not MODEL_NAME:
        raise RuntimeError("Missing AGENT_MODEL. Set it to a model supported by your endpoint.")
    return MODEL_NAME


def get_final_verifier(workspace: str | Path | None = None) -> str | None:
    """Load an optional user/project-selected command for the final gate."""

    value = (_setting("AGENT_FINAL_VERIFIER", "") or "").strip()
    if value:
        if len(value) > 2_000:
            raise RuntimeError("AGENT_FINAL_VERIFIER must be at most 2000 characters")
        return value

    root = Path(workspace or WORKSPACE_DIR).resolve()
    path = root / ".agent-verifier"
    if not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) > 2_000 or b"\x00" in data:
        raise RuntimeError(".agent-verifier must be a text command of at most 2000 bytes")
    try:
        value = data.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(".agent-verifier must be valid UTF-8 text") from exc
    if not value:
        raise RuntimeError(".agent-verifier must contain a non-empty command")
    return value
