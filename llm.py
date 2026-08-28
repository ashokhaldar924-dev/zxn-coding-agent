"""One transparent OpenAI-compatible Chat Completions request."""

from __future__ import annotations

import time
from typing import Any

import config


class LLMError(RuntimeError):
    """Raised when the model endpoint cannot provide a valid assistant message."""


def _usage(data: dict[str, Any]) -> dict[str, int]:
    raw = data.get("usage") or {}
    input_tokens = raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0
    output_tokens = raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0
    total_tokens = raw.get("total_tokens", input_tokens + output_tokens) or 0
    if not raw:
        return {}
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(total_tokens),
    }


def _safe_text(text: str) -> str:
    key = config.get_api_key()
    return text.replace(key, "[REDACTED]")[:500]


def call(msgs: list[dict], schemas: list[dict]) -> tuple[dict, dict]:
    """Send one non-streaming request; retry transient failures at most twice."""

    try:
        import requests
    except ImportError as exc:
        raise LLMError("The 'requests' package is required for real model calls.") from exc

    key = config.get_api_key()
    payload = {
        "model": config.get_model(),
        "messages": msgs,
        "tools": schemas,
        "tool_choice": "auto",
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{config.API_BASE_URL}/chat/completions"

    response = None
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
        except requests.RequestException as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise LLMError(
                f"Model request failed: {type(exc).__name__}: {_safe_text(str(exc))}"
            ) from exc

        if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
            time.sleep(0.5 * (attempt + 1))
            continue
        break

    assert response is not None
    if response.status_code != 200:
        raise LLMError(
            f"Model endpoint returned HTTP {response.status_code}: {_safe_text(response.text)}"
        )

    try:
        data = response.json()
        message = data["choices"][0]["message"]
        if not isinstance(message, dict):
            raise TypeError("assistant message is not an object")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise LLMError(
            f"Invalid model response: {exc}. Body: {_safe_text(response.text)}"
        ) from exc
    return message, _usage(data)
