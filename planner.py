"""Small, deterministic task-plan state for non-trivial coding work."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
MAX_PLAN_ITEMS = 8
MAX_STEP_CHARS = 160
MAX_EXPLANATION_CHARS = 400

PLAN_INVESTIGATION_TOOLS = frozenset(
    {"list_dir", "glob_files", "repo_map", "search_text", "read_file"}
)

PLANNER_POLICY_PROMPT = """Planning policy:
- Do not create a plan for a one-file read, a tiny obvious fix, one configuration edit, or a code question. Use a plan only when work has several technical stages and will need multiple tool rounds.
- In an existing repository, investigate before the first plan: use the minimum useful combination of repository listing/map, search, and targeted reads to understand the affected modules and constraints. A new or empty project may be planned directly from the requirements.
- Usually use 3-7 task-specific technical milestones. Split a step when it contains several independently verifiable mechanisms; do not split merely to reach a target count.
- Do not use generic standalone steps such as implement feature, write code, write tests, write README/docs, summarize, or run tests. Tests may be milestones only when they say what behavior or boundary they validate. A final full-suite verification milestone is useful.
- Keep at most one item in_progress. Mark the next milestone in_progress before working on it, mark completed milestones promptly, and call update_plan so the UI reflects real progress rather than jumping from 0/N to N/N.
- Preserve step text when only statuses change. Revise the steps only when repository evidence, a failed test, or a changed implementation approach invalidates the previous route; add or remove only genuinely necessary work.
- Plan completion is navigation state only and never replaces runtime verification.

High-quality plan for a local task scheduler:
1. Define task lifecycle states and the SQLite schema
2. Implement durable writes, restart recovery, and duplicate-execution protection
3. Add scheduling, cancellation, and bounded retry transitions
4. Cover recovery, retry, and invalid-transition boundaries
5. Run the full regression verifier and resolve failures

High-quality plan for a grade-distribution change:
1. Locate the existing grade model and statistics entry points
2. Define score buckets and the distribution result contract
3. Connect distribution queries through the service and CLI
4. Cover boundary scores, empty courses, and missing courses
5. Run the full regression verifier and resolve failures

Low-quality plan (do not use):
1. Implement the feature
2. Write tests
3. Write README
4. Run tests"""

_GENERIC_STEPS = frozenset(
    {
        "implement",
        "implementfeature",
        "writecode",
        "coding",
        "writetests",
        "addtests",
        "writepytest",
        "runtests",
        "runpytest",
        "writereadme",
        "updatereadme",
        "writedocs",
        "updatedocs",
        "summarize",
        "summarizeresults",
        "实现",
        "实现功能",
        "写代码",
        "编写代码",
        "开发功能",
        "写测试",
        "编写测试",
        "补充测试",
        "编写pytest",
        "运行测试",
        "执行测试",
        "运行pytest",
        "编写readme",
        "更新readme",
        "编写文档",
        "更新文档",
        "总结",
        "总结结果",
    }
)


def plan_policy_issue(
    raw_items: object,
    *,
    task: str,
    existing_workspace: bool,
    investigation_tools: set[str],
    initial_plan: bool,
) -> str | None:
    """Return one actionable policy issue without changing ``PlanState``.

    This deliberately catches only high-confidence failure modes. The model is
    still responsible for decomposing the technical work; the Runtime does not
    attempt semantic planning.
    """

    if initial_plan and existing_workspace and len(investigation_tools) < 2:
        return (
            "Investigate the existing repository before creating its first plan. "
            "Use at least two useful read-only discovery tools (for example a "
            "repository map/search and a targeted read), then plan from that evidence."
        )
    if not isinstance(raw_items, list):
        return None  # Structural validation remains PlanState's responsibility.
    steps = [
        raw.get("step", "")
        for raw in raw_items
        if isinstance(raw, dict) and isinstance(raw.get("step"), str)
    ]
    if len(steps) != len(raw_items):
        return None
    docs_requested = _task_requests_documentation(task)
    for step in steps:
        if _compact(step) in _GENERIC_STEPS:
            if docs_requested and _is_document_delivery(step):
                continue
            return (
                "Plan steps must name a task-specific technical outcome, not a "
                f"generic activity: {step!r}."
            )
        if _is_document_delivery(step) and not docs_requested:
            return (
                "README/documentation must not be a standalone plan milestone "
                "unless documentation is a primary user objective."
            )
    if len(steps) >= 3 and not docs_requested:
        core_steps = [step for step in steps if not _is_support_step(step)]
        if len(core_steps) < 2:
            return (
                "This plan is still organized around testing/documentation rather "
                "than technical milestones. Split the core work into at least two "
                "independently meaningful outcomes."
            )
    return None


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def _task_requests_documentation(task: str) -> bool:
    lowered = task.casefold()
    negative = re.search(
        r"(?:do\s+not|don't|without|不要|无需|不用).{0,20}(?:readme|docs?|documentation|文档|说明)",
        lowered,
    )
    if negative:
        return False
    return bool(
        re.search(
            r"(?:write|update|create|add|improve|rewrite).{0,30}(?:readme|docs?|documentation)"
            r"|(?:编写|更新|创建|新增|完善|重写).{0,20}(?:readme|文档|说明)",
            lowered,
        )
    )


def _is_document_delivery(step: str) -> bool:
    lowered = step.casefold()
    return bool(
        re.search(
            r"(?:write|update|create|add|document).{0,30}(?:readme|docs?|documentation)"
            r"|(?:编写|更新|创建|新增|补充).{0,20}(?:readme|文档|说明)",
            lowered,
        )
    )


def _is_support_step(step: str) -> bool:
    lowered = step.casefold()
    return bool(
        re.search(
            r"\b(?:test|tests|pytest|verify|verification|regression|readme|docs?|documentation)\b"
            r"|测试|验证|回归|readme|文档|说明",
            lowered,
        )
    )


@dataclass(frozen=True)
class PlanItem:
    step: str
    status: str

    def to_data(self) -> dict[str, str]:
        return {"step": self.step, "status": self.status}


@dataclass
class PlanState:
    """Current navigation plan; never a completion or verification gate."""

    items: list[PlanItem] = field(default_factory=list)
    explanation: str | None = None
    revision: int = 0

    def replace(
        self,
        raw_items: object,
        *,
        explanation: object = None,
        preserve_explanation: bool = False,
    ) -> bool:
        items = _parse_items(raw_items)
        next_explanation = (
            self.explanation
            if preserve_explanation
            else _optional_text(explanation, "explanation", MAX_EXPLANATION_CHARS)
        )
        if self.items == items and self.explanation == next_explanation:
            return False
        self.items = items
        self.explanation = next_explanation
        self.revision += 1
        return True

    @property
    def completed(self) -> int:
        return sum(item.status == "completed" for item in self.items)

    def progress_text(self) -> str:
        if not self.items:
            return "none"
        active = next(
            (item.step for item in self.items if item.status == "in_progress"),
            None,
        )
        suffix = f"; active: {_short(active, 72)}" if active else ""
        return f"{self.completed}/{len(self.items)} completed (plan r{self.revision}){suffix}"

    def compact(self) -> str:
        if not self.items:
            return "plan: none"
        symbols = {"pending": "○", "in_progress": "→", "completed": "✓"}
        steps = " | ".join(
            f"{symbols[item.status]} {_short(item.step, 72)}" for item in self.items
        )
        return (
            f"plan r{self.revision}: {self.completed}/{len(self.items)} completed; "
            f"{steps}"
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "explanation": self.explanation,
            "items": [item.to_data() for item in self.items],
        }

    @classmethod
    def from_data(cls, value: object) -> PlanState:
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise TypeError("plan state must be an object")
        revision = value.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("plan revision must be a non-negative integer")
        raw_items = value.get("items", [])
        if raw_items == []:
            items: list[PlanItem] = []
        else:
            items = _parse_items(raw_items)
        explanation = _optional_text(
            value.get("explanation"), "explanation", MAX_EXPLANATION_CHARS
        )
        return cls(items=items, explanation=explanation, revision=revision)


def _parse_items(value: object) -> list[PlanItem]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PLAN_ITEMS:
        raise ValueError(f"plan must contain 1-{MAX_PLAN_ITEMS} items")
    items: list[PlanItem] = []
    active = 0
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise TypeError(f"plan[{index}] must be an object")
        step = _required_text(raw.get("step"), f"plan[{index}].step", MAX_STEP_CHARS)
        status = raw.get("status")
        if not isinstance(status, str) or status not in PLAN_STATUSES:
            allowed = ", ".join(sorted(PLAN_STATUSES))
            raise ValueError(f"plan[{index}].status must be one of: {allowed}")
        active += status == "in_progress"
        items.append(PlanItem(step=step, status=str(status)))
    if active > 1:
        raise ValueError("at most one plan item may be in_progress")
    return items


def _required_text(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{field_name} must not exceed {limit} characters")
    return text


def _optional_text(value: object, field_name: str, limit: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name, limit)


def _short(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"
