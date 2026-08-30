"""Bounded model context made of indivisible logical message groups."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class ContextStats:
    before_chars: int = 0
    after_chars: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    reserved_tokens: int = 0
    estimated_window_tokens: int = 0
    pruned_tool_outputs: int = 0
    dropped_groups: int = 0
    over_budget: bool = False


def _encoded(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _message_chars(messages: list[dict]) -> int:
    return len(_encoded(messages).decode("utf-8"))


def _message_tokens(messages: list[dict]) -> int:
    """Provider-independent approximation used only for context budgeting."""

    return math.ceil(len(_encoded(messages)) / 4)


def estimate_tokens(value: object) -> int:
    """Estimate serialized request tokens with the same deterministic heuristic."""

    return math.ceil(len(_encoded(value)) / 4)


def _flatten(groups: list[list[dict]]) -> list[dict]:
    return [message for group in groups for message in group]


class Ctx:
    """Preserve full session history while building a bounded model view.

    ``history_groups`` contains completed earlier user turns. ``head[1]`` is
    the current user task and ``groups`` contains its assistant/tool rounds.
    This keeps the active goal anchored without pretending the whole durable
    session must fit in every request.
    """

    def __init__(self, system_prompt: str, user_task: str):
        self.head: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self.history_groups: list[list[dict]] = []
        self.groups: list[list[dict]] = []
        self.active = True
        self.last_stats = ContextStats()

    @property
    def current_task(self) -> str:
        return str(self.head[1].get("content") or "") if self.active else ""

    def add_group(self, messages: list[dict]) -> None:
        if not self.active:
            raise ValueError("cannot add a model group without an active user task")
        if not messages or not all(isinstance(message, dict) for message in messages):
            raise ValueError("a context group must be a non-empty list of messages")
        # Keep the complete in-process transcript. build() creates the bounded
        # model view; durable sessions store these full logical groups.
        self.groups.append(deepcopy(messages))

    def archive_current(self) -> None:
        if not self.active:
            return
        self.history_groups.append([deepcopy(self.head[1])])
        self.history_groups.extend(deepcopy(self.groups))
        self.groups = []
        self.active = False

    def start_task(self, user_task: str) -> None:
        task = user_task.strip()
        if not task:
            raise ValueError("user task must not be empty")
        self.archive_current()
        self.head[1] = {"role": "user", "content": task}
        self.groups = []
        self.active = True

    def add_between_turn_group(self, messages: list[dict]) -> None:
        """Add user-owned context, such as opted-in ``!command`` output."""

        if not messages or not all(isinstance(message, dict) for message in messages):
            raise ValueError("a history group must be a non-empty list of messages")
        self.archive_current()
        self.history_groups.append(deepcopy(messages))

    def _assemble(
        self,
        history: list[list[dict]],
        current: list[list[dict]],
        runtime_state: str | None,
    ) -> list[dict]:
        system = deepcopy(self.head[0])
        if runtime_state:
            system["content"] = str(system.get("content") or "") + "\n\n" + runtime_state
        messages = [system, *_flatten(history)]
        if self.active:
            messages.append(deepcopy(self.head[1]))
        messages.extend(_flatten(current))
        return messages

    @staticmethod
    def _over_budget(messages: list[dict], reserved_tokens: int) -> bool:
        return (
            _message_chars(messages) > config.MAX_CONTEXT_CHARS
            or _message_tokens(messages) + reserved_tokens > config.MAX_CONTEXT_TOKENS
        )

    def build(
        self,
        runtime_state: str | None = None,
        *,
        reserved_tokens: int = 0,
    ) -> list[dict]:
        if not isinstance(reserved_tokens, int) or reserved_tokens < 0:
            raise ValueError("reserved_tokens must be a non-negative integer")
        # MAX_GROUPS limits only the model view. The durable transcript remains
        # complete in history_groups/groups and in the session JSONL.
        current = deepcopy(self.groups[-config.MAX_GROUPS :])
        remaining = max(0, config.MAX_GROUPS - len(current))
        history = deepcopy(self.history_groups[-remaining:]) if remaining else []

        messages = self._assemble(history, current, runtime_state)
        before_chars = _message_chars(messages)
        before_tokens = _message_tokens(messages)
        pruned = 0
        dropped = 0

        protected_from = max(0, len(current) - config.CONTEXT_KEEP_FULL_GROUPS)
        for group in [*history, *current[:protected_from]]:
            if not self._over_budget(
                self._assemble(history, current, runtime_state), reserved_tokens
            ):
                break
            for message in group:
                if message.get("role") != "tool":
                    continue
                content = message.get("content")
                if not isinstance(content, str) or content.startswith("[older tool output pruned:"):
                    continue
                output_ref = re.search(r"\bcmd-[0-9a-f]{12}\.txt\b", content)
                saved = (
                    f"; full command output remains available as {output_ref.group(0)} "
                    "via read_command_output"
                    if output_ref
                    else "; re-run the tool if exact details are still needed"
                )
                message["content"] = (
                    f"[older tool output pruned: {len(content)} characters{saved}.]"
                )
                pruned += 1
                if not self._over_budget(
                    self._assemble(history, current, runtime_state), reserved_tokens
                ):
                    break

        while history and self._over_budget(
            self._assemble(history, current, runtime_state), reserved_tokens
        ):
            history.pop(0)
            dropped += 1

        while (
            len(current) > config.CONTEXT_KEEP_FULL_GROUPS
            and self._over_budget(
                self._assemble(history, current, runtime_state), reserved_tokens
            )
        ):
            current.pop(0)
            dropped += 1

        messages = self._assemble(history, current, runtime_state)
        after_chars = _message_chars(messages)
        after_tokens = _message_tokens(messages)
        self.last_stats = ContextStats(
            before_chars=before_chars,
            after_chars=after_chars,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            reserved_tokens=reserved_tokens,
            estimated_window_tokens=after_tokens + reserved_tokens,
            pruned_tool_outputs=pruned,
            dropped_groups=dropped,
            over_budget=self._over_budget(messages, reserved_tokens),
        )
        return messages
