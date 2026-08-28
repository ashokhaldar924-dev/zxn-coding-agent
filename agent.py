"""Transparent local coding-agent loop and command-line entry point."""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from typing import Callable

import config
from ctx import Ctx
from gitguard import GitGuard
import llm
from log import NullLog, RunLog
from state import State, ToolRes
import tools
import ui


def system_prompt() -> str:
    return f"""You are a coding agent working inside a local project at {config.WORKSPACE_DIR}.

Inspect the repository before changing code. Use read_file, list_dir, and search_text to gather evidence instead of guessing. For small edits to existing files, prefer edit_file over whole-file write_file. Writes and commands may require user approval; if rejected, adjust rather than repeating blindly. Commands already start in the workspace on platform {sys.platform}; do not prepend cd, and use commands available on that platform. Never inspect or modify the private .agent trajectory directory while solving the task. Use run_command for exploration and check_command when validating the current code revision. If a command fails, inspect its output and continue fixing when appropriate. Do not claim success without evidence. When finished, briefly state what changed, which files changed, and how it was verified."""


ModelCall = Callable[[list[dict], list[dict]], tuple[dict, dict]]


def _assistant_entry(message: dict, step: int) -> tuple[dict, list[tuple[dict, str | None]]]:
    """Normalize tool calls so every result can be linked to a unique call id."""

    entry = {"role": "assistant", "content": message.get("content") or ""}
    # DeepSeek thinking-mode tool calls require this field to be passed back
    # unchanged on the next request. Providers that do not return it are
    # unaffected, so the core protocol stays OpenAI-compatible.
    if message.get("reasoning_content") is not None:
        entry["reasoning_content"] = message["reasoning_content"]
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        return entry, [({}, "tool_calls must be a list")]

    calls: list[tuple[dict, str | None]] = []
    normalized: list[dict] = []
    used_ids: set[str] = set()
    for index, raw in enumerate(raw_calls, 1):
        error = None
        if not isinstance(raw, dict):
            raw = {}
            error = "tool call must be an object"
        call_id = raw.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in used_ids:
            call_id = f"runtime-call-{step}-{index}"
            error = error or "tool call had a missing or duplicate id"
        used_ids.add(call_id)
        function = raw.get("function")
        if not isinstance(function, dict):
            function = {}
            error = error or "tool call function must be an object"
        name = function.get("name")
        if not isinstance(name, str) or not name:
            name = "__invalid_tool_call__"
            error = error or "tool call function name is missing"
        arguments = function.get("arguments", "{}")
        call = {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        normalized.append(call)
        calls.append((call, error))
    if normalized:
        entry["tool_calls"] = normalized
    return entry, calls


def _parse_args(call: dict) -> tuple[dict | None, ToolRes | None]:
    raw = call["function"].get("arguments", "{}")
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, ToolRes("Tool arguments must be a JSON string or object.", ok=False)
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, ToolRes(f"Could not parse tool arguments as JSON: {exc}", ok=False)
    if not isinstance(args, dict):
        return None, ToolRes("Tool arguments JSON must decode to an object.", ok=False)
    return args, None


def _add_usage(st: State, usage: dict) -> None:
    if not usage:
        return
    in_tok = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    out_tok = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    st.in_tok += in_tok
    st.out_tok += out_tok
    print(f"[tokens] in={in_tok} out={out_tok} total={in_tok + out_tok}")


def _stop(logger, kind: str, text: str, st: State) -> str:
    logger.event(kind, message=text, step=st.step, revision=st.rev, verified_revision=st.ok_rev)
    ui.warning(text)
    return text


def run_task(
    ctx: Ctx,
    *,
    st: State | None = None,
    model_call: ModelCall | None = None,
    logger=None,
) -> str:
    """Run until a final answer passes the gate or a runtime limit stops the loop."""

    st = st or State()
    model_call = model_call or llm.call
    logger = logger or NullLog()

    try:
        for step in range(1, config.MAX_STEPS + 1):
            st.step = step
            if time.time() - st.start >= config.MAX_TIME:
                return _stop(logger, "max_time", f"Stopped after {config.MAX_TIME:g} seconds.", st)

            try:
                message, usage = model_call(ctx.build(), tools.TOOL_SCHEMAS)
            except llm.LLMError as exc:
                logger.event("fatal_error", error=type(exc).__name__, message=str(exc), step=step)
                raise
            if not isinstance(message, dict):
                message = {"content": "", "tool_calls": "invalid"}

            _add_usage(st, usage or {})
            entry, calls = _assistant_entry(message, step)
            logger.event("model_response", step=step, message=entry, usage=usage or {})

            if calls:
                # A non-list tool_calls value produces one parser observation without a tool id.
                if calls == [({}, "tool_calls must be a list")]:
                    feedback = "[Runtime] Invalid model response: tool_calls must be a list."
                    ctx.add_group([entry, {"role": "user", "content": feedback}])
                    logger.event("tool_parse_error", step=step, message=feedback)
                    st.errs += 1
                else:
                    group = [entry]
                    for call, shape_error in calls:
                        name = call["function"]["name"]
                        raw_args = call["function"].get("arguments", "{}")
                        ui.tool(name, str(raw_args)[:200])
                        logger.event(
                            "tool_call",
                            step=step,
                            id=call["id"],
                            name=name,
                            arguments=raw_args,
                        )
                        if shape_error:
                            result = ToolRes(f"Invalid tool call: {shape_error}", ok=False)
                        else:
                            args, parse_error = _parse_args(call)
                            result = parse_error or tools.run_tool(name, args or {}, st)
                        group.append(
                            {"role": "tool", "tool_call_id": call["id"], "content": result.text}
                        )
                        logger.event(
                            "tool_result",
                            step=step,
                            id=call["id"],
                            name=name,
                            text=result.text,
                            ok=result.ok,
                            rc=result.rc,
                            rejected=result.rejected,
                            blocked=result.blocked,
                            revision=st.rev,
                            verified_revision=st.ok_rev,
                        )
                        if result.rejected:
                            logger.event("user_rejection", step=step, id=call["id"], name=name)
                        if result.blocked:
                            logger.event("permission_block", step=step, id=call["id"], name=name)
                        st.errs = 0 if result.ok else st.errs + 1
                    ctx.add_group(group)

                if st.errs >= config.MAX_ERRORS:
                    return _stop(
                        logger,
                        "max_errors",
                        f"Stopped after {st.errs} consecutive runtime/tool errors.",
                        st,
                    )
                continue

            final_text = entry.get("content") or "(model returned no content)"
            if st.changed and st.ok_rev != st.rev:
                feedback = (
                    "[Runtime] Current workspace revision has not been successfully verified. "
                    "Use check_command before finishing."
                )
                ctx.add_group([entry, {"role": "user", "content": feedback}])
                logger.event(
                    "verification_gate",
                    step=step,
                    accepted=False,
                    revision=st.rev,
                    verified_revision=st.ok_rev,
                )
                ui.warning(feedback)
                continue

            ctx.add_group([entry])
            logger.event(
                "verification_gate",
                step=step,
                accepted=True,
                revision=st.rev,
                verified_revision=st.ok_rev,
            )
            logger.event(
                "final",
                step=step,
                text=final_text,
                files=sorted(st.files),
                revision=st.rev,
                verified_revision=st.ok_rev,
                input_tokens=st.in_tok,
                output_tokens=st.out_tok,
            )
            return final_text

        return _stop(logger, "max_steps", f"Stopped after {config.MAX_STEPS} steps.", st)
    except KeyboardInterrupt:
        return _stop(logger, "interrupted", "Stopped by user (Ctrl+C).", st)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A small local tool-calling coding agent.")
    parser.add_argument("task", nargs="*", help="Programming task; prompted when omitted.")
    parser.add_argument("--yes", action="store_true", help="Disable effect-tool confirmation for this run.")
    parser.add_argument("--workspace", help="Workspace directory for this run.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.workspace:
        config.WORKSPACE_DIR = str(Path(args.workspace).expanduser().resolve())
    if args.yes:
        config.REQUIRE_CONFIRMATION = False
        ui.warning("Confirmation disabled for this run.")

    try:
        config.get_api_key()
        config.get_model()
    except RuntimeError as exc:
        ui.error(f"Configuration error: {exc}")
        return 2
    task = " ".join(args.task).strip() if args.task else input("Programming task:\n> ").strip()
    if not task:
        print("No task supplied.")
        return 2

    git_guard = GitGuard.scan(config.WORKSPACE_DIR)
    st = State(git_guard=git_guard)
    initial_dirty = git_guard.display_paths(config.WORKSPACE_DIR)
    if initial_dirty:
        ui.warning(
            "Git guard: preserving pre-existing changes in "
            + ", ".join(initial_dirty[:10])
            + (" ..." if len(initial_dirty) > 10 else "")
        )
    logger = RunLog()
    logger.event(
        "task",
        text=task,
        workspace=config.WORKSPACE_DIR,
        model=config.MODEL_NAME,
        initial_dirty=initial_dirty,
    )
    ctx = Ctx(system_prompt(), task)
    try:
        final = run_task(ctx, st=st, logger=logger)
    except llm.LLMError as exc:
        ui.error(f"Fatal model error: {exc}")
        return 1

    print(f"\n[final] {final}")
    print(
        f"[runtime] changed={sorted(st.files)} revision={st.rev} "
        f"verified_revision={st.ok_rev} tokens={st.in_tok + st.out_tok}"
    )
    print(f"[trajectory] {logger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
