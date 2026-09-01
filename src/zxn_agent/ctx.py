"""Bounded model context made of indivisible logical message groups."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass

from . import config

MAX_RETAINED_GROUP_OVERHEAD_CHARS = 6_000


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


def _tool_names_by_id(group: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in group:
        for call in message.get("tool_calls", []):
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            function = call.get("function")
            if isinstance(call_id, str) and isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str):
                    names[call_id] = name
    return names


def _group_has_retained_payload(group: list[dict], payloads: set[str]) -> bool:
    return any(
        message.get("role") == "tool" and message.get("content") in payloads
        for message in group
    )


def _retained_group_selection(
    groups: list[list[dict]],
    payloads: set[str],
) -> tuple[set[int], set[str]]:
    """Pin economical original groups without carrying huge reasoning overhead."""

    latest: dict[str, int] = {}
    for index, group in enumerate(groups):
        for message in group:
            content = message.get("content")
            if message.get("role") == "tool" and content in payloads:
                latest[str(content)] = index
    by_group: dict[int, set[str]] = {}
    for payload, index in latest.items():
        by_group.setdefault(index, set()).add(payload)
    selected_indexes: set[int] = set()
    selected_payloads: set[str] = set()
    for index in sorted(by_group, reverse=True):
        group_payloads = by_group[index]
        source_group = groups[index]
        reasoning_chars = sum(
            len(str(message.get("reasoning_content") or ""))
            for message in source_group
        )
        overhead = _message_chars(source_group) - sum(
            len(payload) for payload in group_payloads
        )
        if (
            reasoning_chars <= MAX_RETAINED_GROUP_OVERHEAD_CHARS
            and overhead <= MAX_RETAINED_GROUP_OVERHEAD_CHARS
        ):
            selected_indexes.add(index)
            selected_payloads.update(group_payloads)
    return selected_indexes, selected_payloads


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
        self.last_retained_evidence: frozenset[str] = frozenset()

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
        messages = [deepcopy(self.head[0])]
        if runtime_state:
            messages[0]["content"] = (
                f"{messages[0].get('content', '')}\n\n{runtime_state}"
            )
        messages.extend(_flatten(history))
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
        retained_evidence: tuple[str, ...] | list[str] | None = None,
        reserved_tokens: int = 0,
    ) -> list[dict]:
        if not isinstance(reserved_tokens, int) or reserved_tokens < 0:
            raise ValueError("reserved_tokens must be a non-negative integer")
        # MAX_GROUPS bounds the ordinary recent view. A small number of original
        # read_file groups may remain pinned while State retains their exact
        # payloads; this preserves provider fields such as reasoning_content.
        evidence = set(retained_evidence or ())
        recent_start = max(0, len(self.groups) - config.MAX_GROUPS)
        selected_indexes = set(range(recent_start, len(self.groups)))
        pinned_indexes, pinned_payloads = _retained_group_selection(
            self.groups,
            evidence,
        )
        selected_indexes.update(pinned_indexes)
        current = [deepcopy(self.groups[index]) for index in sorted(selected_indexes)]
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
            tool_names = _tool_names_by_id(group)
            for message in group:
                if message.get("role") != "tool":
                    continue
                content = message.get("content")
                if not isinstance(content, str) or content.startswith("[older tool output pruned:"):
                    continue
                if content in pinned_payloads:
                    continue
                output_ref = re.search(r"\bcmd-[0-9a-f]{12}\.txt\b", content)
                tool_name = tool_names.get(str(message.get("tool_call_id") or ""))
                if output_ref:
                    saved = (
                        f"; full command output remains available as {output_ref.group(0)} "
                        "via read_command_output"
                    )
                elif tool_name == "read_file":
                    saved = (
                        "; reuse it from the Runtime exact-file working set when "
                        "present; otherwise request only the smallest missing snippet"
                    )
                else:
                    saved = "; use a targeted tool call only if exact details are still needed"
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

        while self._over_budget(
            self._assemble(history, current, runtime_state), reserved_tokens
        ):
            protected_from = max(0, len(current) - config.CONTEXT_KEEP_FULL_GROUPS)
            removable = next(
                (
                    index
                    for index, group in enumerate(current[:protected_from])
                    if not _group_has_retained_payload(group, pinned_payloads)
                ),
                None,
            )
            if removable is None:
                break
            current.pop(removable)
            dropped += 1

        messages = self._assemble(history, current, runtime_state)
        after_chars = _message_chars(messages)
        after_tokens = _message_tokens(messages)
        self.last_retained_evidence = frozenset(
            str(message.get("content"))
            for message in messages
            if message.get("role") == "tool" and message.get("content") in evidence
        )
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
