"""Bounded model context made of indivisible logical message groups."""

from __future__ import annotations

from copy import deepcopy

import config


class Ctx:
    """Keep the system/task anchors and recent complete interaction groups."""

    def __init__(self, system_prompt: str, user_task: str):
        self.head: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_task},
        ]
        self.groups: list[list[dict]] = []

    def add_group(self, messages: list[dict]) -> None:
        if not messages or not all(isinstance(message, dict) for message in messages):
            raise ValueError("a context group must be a non-empty list of messages")
        self.groups.append(deepcopy(messages))
        if len(self.groups) > config.MAX_GROUPS:
            self.groups = self.groups[-config.MAX_GROUPS :]

    def build(self) -> list[dict]:
        messages = deepcopy(self.head)
        for group in self.groups:
            messages.extend(deepcopy(group))
        return messages
