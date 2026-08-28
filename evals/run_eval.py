"""Run opt-in real-model coding tasks and record reproducible runtime metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

try:
    from evals.cases import CASES
except ModuleNotFoundError:
    from cases import CASES


ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"
VERIFIER = f'"{sys.executable}" -m unittest discover -s tests -v'


def _hash_tests(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((workspace / "tests").glob("**/*"))
        if path.is_file()
    }


def materialize(case: dict, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for relative, content in case["files"].items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    (workspace / "AGENTS.md").write_text(
        "Do not modify tests. Diagnose the implementation and use check_command for the final verifier.\n",
        encoding="utf-8",
        newline="\n",
    )
    (workspace / ".agent-verifier").write_text(VERIFIER + "\n", encoding="utf-8", newline="\n")


def _run_verifier(workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        VERIFIER,
        shell=True,
        cwd=workspace,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
    )


def _trajectory_metrics(workspace: Path) -> dict:
    logs = sorted((workspace / ".agent").glob("run-*.jsonl"))
    if not logs:
        return {"trajectory": None, "tool_calls": 0, "steps": 0, "tokens": 0}
    path = logs[-1]
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    final = next((event for event in reversed(events) if event.get("event") == "final"), {})
    checks = [
        event
        for event in events
        if event.get("event") == "tool_result" and event.get("name") == "check_command"
    ]
    return {
        "trajectory": str(path.relative_to(workspace)),
        "tool_calls": sum(event.get("event") == "tool_call" for event in events),
        "tool_errors": sum(
            event.get("event") == "tool_result" and event.get("ok") is False
            for event in events
        ),
        "failed_checks": sum(event.get("rc") not in {None, 0} for event in checks),
        "steps": max(
            [int(event.get("step", 0)) for event in events if event.get("event") == "model_response"],
            default=0,
        ),
        "tokens": int(final.get("input_tokens", 0)) + int(final.get("output_tokens", 0)),
        "changed_files": final.get("files", []),
        "final_revision": final.get("revision"),
        "verified_revision": final.get("verified_revision"),
        "has_final": bool(final),
    }


def run_case(case: dict, base: Path, dry_run: bool) -> dict:
    workspace = base / case["name"]
    materialize(case, workspace)
    test_hashes = _hash_tests(workspace)
    baseline = _run_verifier(workspace)
    result = {
        "name": case["name"],
        "baseline_exit": baseline.returncode,
        "fixture_valid": baseline.returncode != 0,
    }
    if dry_run:
        return result

    env = os.environ.copy()
    env["AGENT_CONFIRM"] = "false"
    env["AGENT_FINAL_VERIFIER"] = VERIFIER
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(AGENT), "--yes", "--workspace", str(workspace), case["task"]],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=900,
        check=False,
    )
    duration = time.monotonic() - started
    final_check = _run_verifier(workspace)
    tests_unchanged = _hash_tests(workspace) == test_hashes
    metrics = _trajectory_metrics(workspace)
    result.update(metrics)
    result.update({
        "agent_exit": proc.returncode,
        "duration_seconds": round(duration, 3),
        "final_verifier_exit": final_check.returncode,
        "tests_unchanged": tests_unchanged,
        "success": bool(
            result["fixture_valid"]
            and proc.returncode == 0
            and final_check.returncode == 0
            and tests_unchanged
            and metrics.get("has_final")
            and metrics.get("final_revision") == metrics.get("verified_revision")
        ),
        "failure_recovery": metrics.get("failed_checks", 0) > 0 and final_check.returncode == 0,
        "stdout_tail": proc.stdout[-2_000:],
        "stderr_tail": proc.stderr[-2_000:],
    })
    key = os.environ.get("AGENT_API_KEY", "")
    if key:
        result["stdout_tail"] = result["stdout_tail"].replace(key, "[REDACTED]")
        result["stderr_tail"] = result["stderr_tail"].replace(key, "[REDACTED]")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", dest="cases", help="Run only a named case; repeatable.")
    parser.add_argument("--limit", type=int, default=len(CASES))
    parser.add_argument("--dry-run", action="store_true", help="Validate fixtures without calling a model.")
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    selected = [case for case in CASES if not args.cases or case["name"] in set(args.cases)]
    selected = selected[: max(0, args.limit)]
    if not selected:
        parser.error("no evaluation cases selected")
    if not args.dry_run:
        missing = [name for name in ("AGENT_API_KEY", "AGENT_MODEL") if not os.environ.get(name)]
        if missing:
            parser.error("missing environment variables: " + ", ".join(missing))

    temp_root = Path(tempfile.mkdtemp(prefix="zxn-agent-eval-"))
    try:
        results = [run_case(case, temp_root, args.dry_run) for case in selected]
        report = {
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "dry_run": args.dry_run,
            "model": os.environ.get("AGENT_MODEL", "") if not args.dry_run else None,
            "cases": results,
            "summary": {
                "total": len(results),
                "fixture_valid": sum(result["fixture_valid"] for result in results),
                "successes": sum(result.get("success", False) for result in results),
                "success_rate": (
                    sum(result.get("success", False) for result in results) / len(results)
                    if not args.dry_run
                    else None
                ),
                "total_tokens": sum(result.get("tokens", 0) for result in results),
                "tool_calls": sum(result.get("tool_calls", 0) for result in results),
                "failure_recoveries": sum(result.get("failure_recovery", False) for result in results),
            },
        }
        output = args.output
        if output is None:
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            output = ROOT / "evals" / "results" / f"eval-{stamp}.json"
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"report: {output}")
        return 0 if report["summary"]["fixture_valid"] == len(results) else 1
    finally:
        if args.keep_workspaces:
            print(f"workspaces: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
