"""Bounded model context made of indivisible logical message groups."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json

import config


@dataclass(frozen=True)
class ContextStats:
    before_chars: int = 0
    after_chars: int = 0
    pruned_tool_outputs: int = 0
    dropped_groups: int = 0
    over_budget: bool = False


def _message_chars(messages: list[dict]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


def _flatten(head: list[dict], groups: list[list[dict]]) -> list[dict]:
    return head + [message for group in groups for message in group]


class Ctx:
    """Keep the system/task anchors and recent complete interaction groups."""

    def __init__(self, system_prompt: str, user_task: str):
        self.head: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self.groups: list[list[dict]] = []
        self.last_stats = ContextStats()

    def add_group(self, messages: list[dict]) -> None:
        if not messages or not all(isinstance(message, dict) for message in messages):
            raise ValueError("a context group must be a non-empty list of messages")
        self.groups.append(deepcopy(messages))
        if len(self.groups) > config.MAX_GROUPS:
            self.groups = self.groups[-config.MAX_GROUPS :]

    def build(self) -> list[dict]:
        head = deepcopy(self.head)
        groups = deepcopy(self.groups)
        before = _message_chars(_flatten(head, groups))
        budget = config.MAX_CONTEXT_CHARS
        pruned = 0
        dropped = 0

        protected_from = max(0, len(groups) - config.CONTEXT_KEEP_FULL_GROUPS)
        for group in groups[:protected_from]:
            if _message_chars(_flatten(head, groups)) <= budget:
                break
            for message in group:
                if message.get("role") != "tool":
                    continue
                content = message.get("content")
                if not isinstance(content, str) or content.startswith("[older tool output pruned:"):
                    continue
                message["content"] = (
                    f"[older tool output pruned: {len(content)} characters; "
                    "re-run the tool if exact details are still needed.]"
                )
                pruned += 1
                if _message_chars(_flatten(head, groups)) <= budget:
                    break

        while (
            len(groups) > config.CONTEXT_KEEP_FULL_GROUPS
            and _message_chars(_flatten(head, groups)) > budget
        ):
            groups.pop(0)
            dropped += 1

        messages = _flatten(head, groups)
        after = _message_chars(messages)
        self.last_stats = ContextStats(
            before_chars=before,
            after_chars=after,
            pruned_tool_outputs=pruned,
            dropped_groups=dropped,
            over_budget=after > budget,
        )
        return messages
