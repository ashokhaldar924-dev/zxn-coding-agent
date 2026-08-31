"""Deterministic, Runtime-owned task evidence reports."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .changes import FileChange
from .state import State

REPORT_VERSION = 1


def build_evidence_report(
    st: State,
    *,
    changes: list[FileChange],
    final_text: str,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Build a serializable report from Runtime state, never from model claims."""

    verification = dict(st.verification_data())
    verification["workspace_fingerprint"] = (
        st.workspace_tracker.fingerprint() or st.ok_workspace_fingerprint
    )
    task = st.planner_task.split("\n\nUser-explicit file references:", 1)[0].strip()
    return {
        "report_version": REPORT_VERSION,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "task": task,
        "plan": st.plan.to_data(),
        "observed_evidence": list(st.task_evidence),
        "changed_files": [change.to_data() for change in changes],
        "verification_attempts": list(st.check_attempts),
        "repair_progress": st.repair_progress,
        "verification": verification,
        "metrics": {
            "model_calls": st.task_model_calls,
            "tool_calls": st.task_tool_calls,
            "checks": len(st.check_attempts),
            "input_tokens": st.task_in_tok,
            "output_tokens": st.task_out_tok,
            "total_tokens": st.task_tokens,
            "steps": st.step,
            "duration_seconds": elapsed_seconds,
        },
        "outcome": {
            "completed": st.completed,
            "termination_reason": st.termination_reason or "unknown",
            # This field is explicitly labelled because its prose originates
            # from the model or a Runtime stop message; all status fields above
            # remain independently computed by Runtime.
            "model_or_runtime_summary": final_text,
        },
    }


def report_markdown(report: dict[str, Any]) -> str:
    """Render a stable human-readable view of an evidence report."""

    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    verification = (
        report.get("verification")
        if isinstance(report.get("verification"), dict)
        else {}
    )
    outcome = report.get("outcome") if isinstance(report.get("outcome"), dict) else {}
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    changes = report.get("changed_files") if isinstance(report.get("changed_files"), list) else []
    attempts = (
        report.get("verification_attempts")
        if isinstance(report.get("verification_attempts"), list)
        else []
    )
    evidence = (
        report.get("observed_evidence")
        if isinstance(report.get("observed_evidence"), list)
        else []
    )
    lines = [
        "# Coding Agent Evidence Report",
        "",
        f"- Created: {report.get('created', '')}",
        f"- Task: {report.get('task', '')}",
        f"- Termination: {outcome.get('termination_reason', 'unknown')}",
        f"- Completed: {bool(outcome.get('completed'))}",
        f"- Repair progress: {report.get('repair_progress', 'not_checked')}",
        "",
        "## Metrics",
        "",
        f"- Model calls: {metrics.get('model_calls', 0)}",
        f"- Tool calls: {metrics.get('tool_calls', 0)}",
        f"- Checks: {metrics.get('checks', 0)}",
        (
            f"- Tokens: {metrics.get('total_tokens', 0)} "
            f"(input {metrics.get('input_tokens', 0)}, output {metrics.get('output_tokens', 0)})"
        ),
        f"- Steps: {metrics.get('steps', 0)}",
        f"- Duration: {metrics.get('duration_seconds', 0)}s",
        "",
        "## Verification",
        "",
        f"- Workspace revision: {verification.get('workspace_revision', 0)}",
        f"- Verified revision: {verification.get('verified_revision', -1)}",
        f"- Current: {bool(verification.get('current'))}",
        f"- Adequate: {bool(verification.get('adequate'))}",
        f"- Scope: {verification.get('verified_scope') or 'none'}",
        f"- Fingerprint matched: {bool(verification.get('fingerprint_matched'))}",
        f"- Workspace fingerprint: {verification.get('workspace_fingerprint') or 'none'}",
        "",
        "## Plan",
        "",
    ]
    for item in plan.get("items", []) if isinstance(plan.get("items"), list) else []:
        if isinstance(item, dict):
            lines.append(f"- [{item.get('status', 'pending')}] {item.get('step', '')}")
    lines.extend(["", "## Changed files", ""])
    for change in changes:
        if isinstance(change, dict):
            lines.append(
                f"- {change.get('kind', 'changed')} {change.get('path', '')} "
                f"(+{change.get('additions', '?')} / -{change.get('deletions', '?')})"
            )
    lines.extend(["", "## Verification attempts", ""])
    for attempt in attempts:
        if isinstance(attempt, dict):
            lines.append(
                f"- step {attempt.get('step', '?')}: rc={attempt.get('rc')} "
                f"scope={attempt.get('scope')} progress={attempt.get('progress')} — "
                f"`{attempt.get('command', '')}`"
            )
    lines.extend(["", "## Observed evidence", "", "```json"])
    lines.append(json.dumps(evidence, ensure_ascii=False, indent=2))
    lines.extend(["```", "", "## Summary", "", str(outcome.get("model_or_runtime_summary", "")), ""])
    return "\n".join(lines)
