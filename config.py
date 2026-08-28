"""Runtime configuration loaded from environment variables only."""

from __future__ import annotations

import os
from pathlib import Path


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false, got {raw!r}")


API_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
MODEL_NAME = os.environ.get("AGENT_MODEL", "")
WORKSPACE_DIR = str(
    Path(os.environ.get("AGENT_WORKSPACE", os.getcwd())).expanduser().resolve()
)

MAX_STEPS = _int("AGENT_MAX_STEPS", 30)
MAX_TIME = _float("AGENT_MAX_TIME", 600.0)
MAX_TOOL_CHARS = _int("AGENT_MAX_TOOL_CHARS", 12_000)
MAX_GROUPS = _int("AGENT_MAX_GROUPS", 8)
MAX_CONTEXT_CHARS = _int("AGENT_MAX_CONTEXT_CHARS", 60_000)
MAX_CONTEXT_TOKENS = _int("AGENT_MAX_CONTEXT_TOKENS", 32_000)
CONTEXT_KEEP_FULL_GROUPS = _int("AGENT_CONTEXT_KEEP_FULL_GROUPS", 1)
if CONTEXT_KEEP_FULL_GROUPS > MAX_GROUPS:
    raise RuntimeError("AGENT_CONTEXT_KEEP_FULL_GROUPS cannot exceed AGENT_MAX_GROUPS")
MAX_PROJECT_CONTEXT_CHARS = _int("AGENT_MAX_PROJECT_CONTEXT_CHARS", 12_000)
MAX_FILE_REFERENCE_CHARS = _int("AGENT_MAX_FILE_REFERENCE_CHARS", 12_000)
CMD_TIMEOUT = _float("AGENT_CMD_TIMEOUT", 60.0)
MAX_ERRORS = _int("AGENT_MAX_ERRORS", 4)
MAX_IDENTICAL_CALLS = _int("AGENT_MAX_IDENTICAL_CALLS", 3, minimum=2)
REQUIRE_CONFIRMATION = _bool("AGENT_CONFIRM", True)


def get_api_key() -> str:
    key = os.environ.get("AGENT_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "Missing AGENT_API_KEY. Set it in the environment before running the agent."
        )
    return key


def get_model() -> str:
    if not MODEL_NAME:
        raise RuntimeError("Missing AGENT_MODEL. Set it to a model supported by your endpoint.")
    return MODEL_NAME


def get_final_verifier(workspace: str | Path | None = None) -> str | None:
    """Load an optional user/project-selected command for the final gate."""

    value = os.environ.get("AGENT_FINAL_VERIFIER", "").strip()
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
