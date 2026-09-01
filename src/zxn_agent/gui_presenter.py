"""Pure event-to-view projection shared by the desktop GUI and its tests."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .changes import FileChange

MAX_DETAIL_LINES = 6
MAX_DETAIL_CHARS = 1_000


@dataclass
class ActivityItem:
    key: str
    kind: str
    title: str
    tone: str = "neutral"
    detail: list[str] = field(default_factory=list)
    change: FileChange | None = None
    output_ref: str | None = None


@dataclass(frozen=True)
class PlanViewItem:
    step: str
    status: str
    evidence: tuple[str, ...] = ()


@dataclass
class VerificationView:
    workspace_revision: int = 0
    verified_revision: int = -1
    required: bool = False
    current: bool = False
    adequate: bool = False
    satisfied: bool = False
    fingerprint_matched: bool = False
    verifier: str | None = None
    last_check_rc: int | None = None
    tracking_complete: bool = True
    required_scope: str = "any"
    verified_scope: str | None = None
    task_completed: bool = False
    progress: str = "not_checked"
    check_attempts: int = 0

    def replace_from_runtime(self, value: object) -> None:
        """Accept only an explicit Runtime snapshot; never infer verification."""

        if not isinstance(value, dict):
            return
        self.workspace_revision = _integer(value.get("workspace_revision"), 0)
        self.verified_revision = _integer(value.get("verified_revision"), -1)
        self.required = value.get("required") is True
        self.current = value.get("current") is True
        self.adequate = value.get("adequate") is True
        self.satisfied = value.get("satisfied") is True
        self.fingerprint_matched = value.get("fingerprint_matched") is True
        verifier = value.get("verifier")
        self.verifier = _safe(verifier) if isinstance(verifier, str) and verifier else None
        rc = value.get("last_check_rc")
        self.last_check_rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
        self.tracking_complete = value.get("tracking_complete") is not False
        required_scope = value.get("required_scope")
        self.required_scope = required_scope if required_scope in {"any", "full"} else "any"
        verified_scope = value.get("verified_scope")
        self.verified_scope = _safe(verified_scope) if isinstance(verified_scope, str) else None
        self.task_completed = value.get("task_completed") is True
        progress = value.get("progress")
        self.progress = _safe(progress) if isinstance(progress, str) else "not_checked"
        self.check_attempts = _integer(value.get("check_attempts"), 0)


@dataclass
class OutcomeView:
    visible: bool = False
    completed: bool = False
    status: str = "none"
    changed_files: int = 0
    additions: int | None = 0
    deletions: int | None = 0
    steps: int = 0
    elapsed_seconds: float | None = None
    text: str = ""
    verifier: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    checks: int = 0
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    reasoning_tokens: int | None = None
    repair_progress: str = "not_checked"
    termination_reason: str | None = None


class GuiPresenter:
    """Project redacted trajectory events into small, deterministic GUI state."""

    def __init__(self) -> None:
        self.workspace = ""
        self.activity: list[ActivityItem] = []
        self.plan: list[PlanViewItem] = []
        self.verification = VerificationView()
        self.outcome = OutcomeView()
        self.final_changes: list[FileChange] = []
        self.evidence_report: dict[str, Any] | None = None
        self._changes: dict[str, FileChange] = {}
        self._pending_tools: dict[str, tuple[str, dict[str, Any]]] = {}
        self._plan_evidence: dict[str, list[str]] = {}

    @property
    def changes(self) -> list[FileChange]:
        return [self._changes[path] for path in sorted(self._changes)]

    def reset(self, workspace: str = "") -> None:
        self.workspace = _safe(workspace)
        self.activity.clear()
        self.plan.clear()
        self.verification = VerificationView()
        self.outcome = OutcomeView()
        self.final_changes.clear()
        self.evidence_report = None
        self._changes.clear()
        self._pending_tools.clear()
        self._plan_evidence.clear()

    def consume(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        if not isinstance(kind, str):
            return
        self.verification.replace_from_runtime(event.get("verification"))

        if kind == "task":
            self.workspace = _safe(event.get("workspace", ""))
            self.final_changes = []
            self._changes.clear()
            self._plan_evidence.clear()
            self.evidence_report = None
            self.outcome = OutcomeView()
            task = _visible_task(_safe(event.get("text", "")))
            self.activity.append(ActivityItem(_key(event, "task"), "task", task, "accent"))
            initial_dirty = event.get("initial_dirty")
            if isinstance(initial_dirty, list) and initial_dirty:
                shown = ", ".join(_safe(path) for path in initial_dirty[:5])
                suffix = " ..." if len(initial_dirty) > 5 else ""
                self.activity.append(
                    ActivityItem(
                        _key(event, "baseline"),
                        "warning",
                        f"任务开始前工作区已有改动：{shown}{suffix}",
                        "warning",
                    )
                )
            self._replace_plan(event.get("plan"))
        elif kind == "model_response":
            message = event.get("message")
            if isinstance(message, dict) and message.get("tool_calls"):
                content = _one_line(message.get("content", ""), 500)
                if content:
                    self.activity.append(
                        ActivityItem(_key(event, "progress"), "progress", content)
                    )
        elif kind == "tool_call":
            self._tool_started(event)
        elif kind == "tool_result":
            self._tool_finished(event)
        elif kind == "plan_update":
            self._replace_plan(event.get("plan"))
        elif kind == "workspace_reconcile":
            paths = event.get("changed_files")
            if isinstance(paths, list) and paths:
                shown = ", ".join(_safe(path) for path in paths[:4])
                self.activity.append(
                    ActivityItem(
                        _key(event, "workspace"),
                        "warning",
                        f"检测到文件工具之外的工作区变更：{shown}",
                        "warning",
                    )
                )
        elif kind == "verification_gate" and event.get("accepted") is False:
            self.activity.append(
                ActivityItem(
                    _key(event, "verification"),
                    "warning",
                    "验证已失效，需要重新执行检查",
                    "warning",
                )
            )
        elif kind == "turn_summary":
            self._turn_summary(event)
        elif kind == "task_restore":
            self._task_restore(event)
        elif kind == "session_resume":
            self.workspace = _safe(event.get("workspace", self.workspace))
            self._replace_plan(event.get("plan"))
            self.activity.append(
                ActivityItem(
                    _key(event, "resume"),
                    "task",
                    "已恢复会话 " + _one_line(event.get("session_id", ""), 80),
                    "accent",
                )
            )
        elif kind in {"fatal_error", "gui_error"}:
            message = _one_line(event.get("message", "运行时错误"), 800)
            self.activity.append(
                ActivityItem(_key(event, "error"), "error", message, "failure")
            )
        elif kind in {
            "max_steps",
            "max_time",
            "max_errors",
            "max_task_tokens",
            "no_progress",
            "incomplete_model_output",
            "content_filter",
            "interrupted",
            "user_stopped",
        }:
            message = _one_line(event.get("message", "任务已停止"), 800)
            self.activity.append(
                ActivityItem(_key(event, "stopped"), "completion", message, "warning")
            )

    def _replace_plan(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            return
        items: list[PlanViewItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            step = raw.get("step")
            status = raw.get("status")
            if isinstance(step, str) and status in {"completed", "in_progress", "pending"}:
                safe_step = _safe(step)
                items.append(
                    PlanViewItem(
                        safe_step,
                        str(status),
                        tuple(self._plan_evidence.get(safe_step, [])),
                    )
                )
        self.plan = items

    def _tool_started(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("id", ""))
        name = str(event.get("name", ""))
        args = _arguments(event.get("arguments"))
        self._pending_tools[call_id] = (name, args)
        if name in {"run_command", "check_command"}:
            label = "正在验证：" if name == "check_command" else "正在运行："
            item = ActivityItem(
                call_id or _key(event, "command"),
                "command",
                f"{label} {_one_line(args.get('cmd', ''), 500)}",
                "running",
            )
            self.activity.append(item)

    def _tool_finished(self, event: dict[str, Any]) -> None:
        call_id = str(event.get("id", ""))
        name, args = self._pending_tools.pop(
            call_id,
            (str(event.get("name", "")), {}),
        )
        text = _safe(event.get("text", ""))
        if name in {"run_command", "check_command"}:
            self._finish_command(call_id, event, text)
        else:
            item = _tool_activity(call_id or _key(event, "tool"), name, args, text, event)
            if item is not None:
                self.activity.append(item)
        for raw in event.get("file_changes", []) if isinstance(event.get("file_changes"), list) else []:
            try:
                change = FileChange.from_data(raw)
            except (TypeError, ValueError):
                continue
            self.activity.append(
                ActivityItem(
                    f"{call_id}:{change.path}",
                    "file",
                    change.path,
                    "accent",
                    change=change,
                )
            )
            self._changes[change.path] = change
        self._attach_plan_evidence(name, args, event)

    def _attach_plan_evidence(
        self,
        name: str,
        args: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        """Attach compact hints derived only from completed Runtime tool events."""

        active_index = next(
            (index for index, item in enumerate(self.plan) if item.status == "in_progress"),
            None,
        )
        if active_index is None or name == "update_plan":
            return
        path = _one_line(args.get("path", ""), 180)
        if name == "read_file":
            hint = f"read_file {path}"
        elif name == "repo_map":
            hint = "repo_map"
        elif name in {"search_text", "glob_files", "list_dir"}:
            hint = name
        elif name in {"write_file", "edit_file", "multi_edit"}:
            hint = f"{name} {path}"
        elif name in {"run_command", "check_command"}:
            rc = event.get("rc")
            hint = f"{name} rc={rc}"
        else:
            return
        step = self.plan[active_index].step
        facts = self._plan_evidence.setdefault(step, [])
        if hint not in facts:
            facts.append(hint)
            del facts[:-2]
        item = self.plan[active_index]
        self.plan[active_index] = PlanViewItem(item.step, item.status, tuple(facts))

    def _finish_command(self, call_id: str, event: dict[str, Any], text: str) -> None:
        item = next((entry for entry in reversed(self.activity) if entry.key == call_id), None)
        if item is None:
            item = ActivityItem(call_id, "command", "命令", "running")
            self.activity.append(item)
        rejected = event.get("rejected") is True or event.get("blocked") is True
        rc = event.get("rc")
        elapsed = event.get("elapsed_seconds")
        elapsed_text = (
            f" · {float(elapsed):.1f}s"
            if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
            else ""
        )
        if rejected:
            item.tone = "warning"
            item.detail = [_one_line(text, 500)]
        elif event.get("ok") is True and rc == 0:
            item.tone = "success"
            item.detail = [(_test_summary(text) or "命令执行成功") + elapsed_text]
        else:
            item.tone = "failure"
            summary = _test_summary(text) or f"命令执行失败（退出码 {rc}）"
            item.detail = [summary + elapsed_text]
            item.detail.extend(
                line for line in _important_lines(text) if line != summary
            )
        output_ref = event.get("output_ref")
        item.output_ref = _safe(output_ref) if isinstance(output_ref, str) else None

    def _turn_summary(self, event: dict[str, Any]) -> None:
        changes: list[FileChange] = []
        raw_changes = event.get("changes")
        if isinstance(raw_changes, list):
            for raw in raw_changes:
                try:
                    changes.append(FileChange.from_data(raw))
                except (TypeError, ValueError):
                    continue
        self.final_changes = changes
        self._changes = {change.path: change for change in changes}
        completed = event.get("completed") is True
        text = _one_line(event.get("text", ""), 700)
        detail = [text] if text else []
        if not completed:
            remaining = sum(item.status != "completed" for item in self.plan)
            if remaining:
                detail.append(f"计划未完成：还剩 {remaining} 项")
        summary = ActivityItem(
            _key(event, "summary"),
            "completion",
            "任务完成" if completed else "任务已停止",
            "success" if completed else "warning",
            detail,
        )
        if (
            not completed
            and self.activity
            and self.activity[-1].kind == "completion"
            and self.activity[-1].tone == "warning"
        ):
            self.activity[-1] = summary
        else:
            self.activity.append(summary)
        additions = (
            sum(change.additions or 0 for change in changes)
            if all(change.additions is not None for change in changes)
            else None
        )
        deletions = (
            sum(change.deletions or 0 for change in changes)
            if all(change.deletions is not None for change in changes)
            else None
        )
        if not completed:
            status = "stopped"
        elif self.verification.current and self.verification.adequate:
            status = "final_verified"
        elif self.verification.current:
            status = "partial"
        elif self.verification.required:
            status = "stale"
        else:
            status = "completed"
        elapsed = event.get("elapsed_seconds")
        report = event.get("report")
        self.evidence_report = dict(report) if isinstance(report, dict) else None
        metrics = (
            report.get("metrics")
            if isinstance(report, dict) and isinstance(report.get("metrics"), dict)
            else {}
        )
        self.outcome = OutcomeView(
            visible=True,
            completed=completed,
            status=status,
            changed_files=len(changes),
            additions=additions,
            deletions=deletions,
            steps=_integer(event.get("steps"), 0),
            elapsed_seconds=(
                float(elapsed)
                if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                else None
            ),
            text=text,
            verifier=self.verification.verifier,
            model_calls=_integer(event.get("model_calls", metrics.get("model_calls")), 0),
            tool_calls=_integer(event.get("tool_calls", metrics.get("tool_calls")), 0),
            checks=_integer(event.get("checks", metrics.get("checks")), 0),
            tokens=_integer(
                metrics.get("total_tokens"),
                _integer(event.get("input_tokens"), 0) + _integer(event.get("output_tokens"), 0),
            ),
            prompt_tokens=_integer(
                metrics.get("prompt_tokens"),
                _integer(event.get("input_tokens"), 0),
            ),
            completion_tokens=_integer(
                metrics.get("completion_tokens"),
                _integer(event.get("output_tokens"), 0),
            ),
            cache_hit_tokens=_optional_integer(metrics.get("prompt_cache_hit_tokens")),
            cache_miss_tokens=_optional_integer(metrics.get("prompt_cache_miss_tokens")),
            reasoning_tokens=_optional_integer(metrics.get("reasoning_tokens")),
            repair_progress=_safe(
                event.get("repair_progress", self.verification.progress)
            ),
            termination_reason=(
                _safe(event.get("termination_reason"))
                if event.get("termination_reason") is not None
                else None
            ),
        )

    def _task_restore(self, event: dict[str, Any]) -> None:
        restored = event.get("restored_paths")
        if isinstance(restored, list):
            restored_paths = {path for path in restored if isinstance(path, str)}
            for path in restored_paths:
                self._changes.pop(path, None)
            self.final_changes = [
                change for change in self.final_changes if change.path not in restored_paths
            ]
        message = _one_line(event.get("message", "已恢复本次任务的改动"), 700)
        self.activity.append(
            ActivityItem(_key(event, "restore"), "completion", message, "warning")
        )
        report = event.get("report")
        self.evidence_report = dict(report) if isinstance(report, dict) else None
        self.outcome.completed = False
        self.outcome.status = "restored"
        self.outcome.text = message
        self.outcome.termination_reason = "restored_task_changes"


def _tool_activity(
    key: str,
    name: str,
    args: dict[str, Any],
    text: str,
    event: dict[str, Any],
) -> ActivityItem | None:
    if event.get("ok") is not True or event.get("rejected") is True or event.get("blocked") is True:
        return ActivityItem(key, "tool", _one_line(text, 600), "warning")
    path = _one_line(args.get("path") or ".", 240)
    if name == "read_file":
        start, end = args.get("start"), args.get("end")
        suffix = f":{start}-{end}" if start and end else f":{start}-" if start else ""
        title = f"已读取 {path}{suffix}"
    elif name == "read_command_output":
        title = f"已读取保存的命令输出 {_one_line(args.get('output_id', ''), 120)}"
    elif name == "search_text":
        title = f'已搜索“{_one_line(args.get("query", ""), 180)}”'
        count = _result_count(text)
        if count is not None:
            title += f" · {count} 个匹配"
    elif name == "repo_map":
        count = _number(text, r"Repo map:\s*(\d+)\s+symbols")
        title = "已检查仓库结构图" + (f" · {count} 个符号" if count is not None else "")
    elif name == "glob_files":
        title = f"已匹配 {_one_line(args.get('pattern', ''), 180)}"
    elif name == "list_dir":
        title = f"已列出 {path}"
    elif name in {"write_file", "edit_file", "multi_edit", "update_plan"}:
        return None
    else:
        title = f"已完成 {name}"
    return ActivityItem(key, "tool", title)


def _arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _visible_task(text: str) -> str:
    return text.split("\n\nUser-explicit file references:", 1)[0].strip()


def _safe(value: object) -> str:
    text = str(value)
    secret = os.environ.get("AGENT_API_KEY", "")
    return text.replace(secret, "[REDACTED]") if secret else text


def _one_line(value: object, limit: int) -> str:
    text = " ".join(_safe(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _test_summary(text: str) -> str | None:
    candidates: list[str] = []
    for line in text.splitlines():
        clean = line.strip().strip("= ")
        if re.search(r"\b\d+\s+(?:passed|failed)\b", clean, re.IGNORECASE):
            candidates.append(clean)
        elif clean == "OK":
            candidates.append("测试通过")
    return _one_line(candidates[-1], 260) if candidates else None


def _important_lines(text: str) -> list[str]:
    lines = [
        _one_line(line, 320)
        for line in text.splitlines()
        if line.strip()
        and line.strip().lower() not in {"stdout:", "stderr:"}
        and not line.lower().startswith("exit code:")
    ]
    pattern = re.compile(r"failed|error|assertion|traceback|exception|timed out", re.IGNORECASE)
    selected = [line for line in lines if pattern.search(line)] or lines[-MAX_DETAIL_LINES:]
    bounded: list[str] = []
    used = 0
    for line in selected[-MAX_DETAIL_LINES:]:
        if used + len(line) > MAX_DETAIL_CHARS:
            break
        bounded.append(line)
        used += len(line)
    return bounded


def _result_count(text: str) -> int | None:
    found = _number(text, r"Found\s+(\d+)\s+matches")
    if found is not None:
        return found
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) if lines else None


def _number(text: str, pattern: str) -> int | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _key(event: dict[str, Any], suffix: str) -> str:
    return f"{event.get('step', 0)}:{suffix}:{len(str(event))}"
