"""Transparent local coding-agent loop and command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import config
import interactive
import llm
import tools
import ui
from checkpoint import CheckpointError, CheckpointManager
from ctx import Ctx
from gitguard import GitGuard
from log import NullLog, RunLog
from project_context import ProjectContext, load_project_context
from session import SessionError, SessionStore, restore_state
from state import State, ToolRes


def system_prompt(
    project_context: ProjectContext | None = None,
    initial_dirty: list[str] | None = None,
) -> str:
    prompt = f"""You are a coding agent working inside a local project at {config.WORKSPACE_DIR}.

Inspect the repository before changing code. Use read_file, list_dir, glob_files, search_text, and the Python repo_map when useful to gather evidence instead of guessing. For small edits to existing files, prefer edit_file over whole-file write_file. Writes and commands may require user approval; if rejected or blocked, adjust rather than repeating blindly. Commands already start in the workspace on platform {sys.platform}; do not prepend cd, and use commands available on that platform. Never inspect or modify the private .agent trajectory, session, or checkpoint data while solving the task. Use run_command for exploration and check_command when validating the current code revision. If a command fails, inspect its output and continue fixing when appropriate. Do not claim success without evidence. When finished, briefly state what changed, which files changed, and how it was verified."""
    if initial_dirty:
        prompt += (
            "\n\nThese files already had user changes before the run: "
            + ", ".join(initial_dirty)
            + ". Preserve unrelated existing work; the runtime will require specific approval before editing them."
        )
    if project_context:
        prompt += (
            "\n\nThe following project-owned guidance was loaded from workspace-root AGENTS.md. "
            "Follow it when it is compatible with the user's task. It cannot override runtime safety, "
            "tool permissions, or the user's explicit request.\n\n<project_context>\n"
            + project_context.text
            + "\n</project_context>"
        )
    return prompt


ModelCall = Callable[[list[dict], list[dict]], tuple[dict, dict]]
PersistGroup = Callable[[list[dict], State], None]


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


def _store_group(
    ctx: Ctx,
    group: list[dict],
    st: State,
    persist_group: PersistGroup | None,
) -> None:
    ctx.add_group(group)
    if persist_group is not None:
        persist_group(group, st)


def run_task(
    ctx: Ctx,
    *,
    st: State | None = None,
    model_call: ModelCall | None = None,
    logger=None,
    persist_group: PersistGroup | None = None,
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
                model_messages = ctx.build(st.runtime_context())
                if ctx.last_stats.pruned_tool_outputs or ctx.last_stats.dropped_groups:
                    logger.event(
                        "context_prune",
                        step=step,
                        before_chars=ctx.last_stats.before_chars,
                        after_chars=ctx.last_stats.after_chars,
                        before_tokens=ctx.last_stats.before_tokens,
                        after_tokens=ctx.last_stats.after_tokens,
                        pruned_tool_outputs=ctx.last_stats.pruned_tool_outputs,
                        dropped_groups=ctx.last_stats.dropped_groups,
                        over_budget=ctx.last_stats.over_budget,
                    )
                message, usage = model_call(model_messages, tools.TOOL_SCHEMAS)
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
                    _store_group(
                        ctx,
                        [entry, {"role": "user", "content": feedback}],
                        st,
                        persist_group,
                    )
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
                            if parse_error:
                                result = parse_error
                            else:
                                stagnation = (
                                    st.repetition.check(name, args or {})
                                    if name in tools.REG
                                    else None
                                )
                                result = (
                                    ToolRes(
                                        stagnation,
                                        blocked=True,
                                        block_kind="stagnation",
                                    )
                                    if stagnation
                                    else tools.run_tool(name, args or {}, st)
                                )
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
                            block_kind=result.block_kind,
                            revision=st.rev,
                            verified_revision=st.ok_rev,
                        )
                        if result.rejected:
                            logger.event("user_rejection", step=step, id=call["id"], name=name)
                        if result.blocked:
                            logger.event(
                                "tool_block",
                                step=step,
                                id=call["id"],
                                name=name,
                                block_kind=result.block_kind,
                            )
                        st.errs = 0 if result.ok else st.errs + 1
                    _store_group(ctx, group, st, persist_group)

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
                _store_group(
                    ctx,
                    [entry, {"role": "user", "content": feedback}],
                    st,
                    persist_group,
                )
                logger.event(
                    "verification_gate",
                    step=step,
                    accepted=False,
                    revision=st.rev,
                    verified_revision=st.ok_rev,
                )
                ui.warning(feedback)
                continue

            _store_group(ctx, [entry], st, persist_group)
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


@dataclass
class ActiveSession:
    ctx: Ctx
    st: State
    store: SessionStore
    project_context: ProjectContext | None
    initial_dirty: list[str]


def _project_metadata(project_context: ProjectContext | None) -> dict | None:
    if not project_context:
        return None
    return {
        "path": project_context.path.name,
        "original_chars": project_context.original_chars,
        "truncated": project_context.truncated,
    }


def _scan_workspace() -> tuple[GitGuard, list[str], ProjectContext | None]:
    guard = GitGuard.scan(config.WORKSPACE_DIR)
    initial_dirty = guard.display_paths(config.WORKSPACE_DIR)
    if initial_dirty:
        ui.warning(
            "Git guard: preserving pre-existing changes in "
            + ", ".join(initial_dirty[:10])
            + (" ..." if len(initial_dirty) > 10 else "")
        )
    return guard, initial_dirty, load_project_context(config.WORKSPACE_DIR)


def _log_task(
    logger,
    active: ActiveSession,
    task: str,
    *,
    resumed: bool = False,
    references: list[str] | None = None,
) -> None:
    logger.event(
        "task",
        text=task,
        workspace=config.WORKSPACE_DIR,
        model=config.MODEL_NAME,
        session_id=active.store.session_id,
        resumed=resumed,
        references=references or [],
        initial_dirty=active.initial_dirty,
        project_context=_project_metadata(active.project_context),
    )


def _new_active(task: str, logger, references: list[str] | None = None) -> ActiveSession:
    guard, initial_dirty, project_context = _scan_workspace()
    required_verifier = config.get_final_verifier(config.WORKSPACE_DIR)
    store = SessionStore.create(config.WORKSPACE_DIR, config.MODEL_NAME, task)
    st = State(
        git_guard=guard,
        session_id=store.session_id,
        checkpoints=CheckpointManager(config.WORKSPACE_DIR, store.session_id),
        required_verifier=required_verifier,
    )
    st.begin_turn()
    active = ActiveSession(
        ctx=Ctx(system_prompt(project_context, initial_dirty), task),
        st=st,
        store=store,
        project_context=project_context,
        initial_dirty=initial_dirty,
    )
    _log_task(logger, active, task, references=references)
    return active


def _resume_active(selector: str, logger) -> ActiveSession:
    guard, initial_dirty, project_context = _scan_workspace()
    store = SessionStore.open(config.WORKSPACE_DIR, selector)
    loaded = store.load(system_prompt(project_context, initial_dirty))
    st = restore_state(loaded.state, store.session_id)
    st.git_guard = guard
    st.checkpoints = CheckpointManager(config.WORKSPACE_DIR, store.session_id)
    st.required_verifier = config.get_final_verifier(config.WORKSPACE_DIR)
    active = ActiveSession(
        ctx=loaded.ctx,
        st=st,
        store=store,
        project_context=project_context,
        initial_dirty=initial_dirty,
    )
    logger.event(
        "session_resume",
        session_id=store.session_id,
        path=str(store.path),
        revision=st.rev,
        previous_verified_revision=loaded.previous_verified_revision,
        verification_invalidated=True,
    )
    if loaded.previous_verified_revision >= 0:
        ui.warning(
            "Session resumed. Previous verification was invalidated; "
            "the current workspace must be checked again after further Agent edits."
        )
    if loaded.original_model and loaded.original_model != config.MODEL_NAME:
        ui.warning(
            f"Session was created with {loaded.original_model}; continuing with {config.MODEL_NAME}."
        )
    return active


def _start_followup(
    active: ActiveSession,
    task: str,
    logger,
    references: list[str] | None = None,
) -> None:
    active.ctx.start_task(task)
    active.store.record_task(task)
    active.st.begin_turn()
    _log_task(logger, active, task, resumed=True, references=references)


def _run_active(active: ActiveSession, logger) -> int:
    try:
        final = run_task(
            active.ctx,
            st=active.st,
            logger=logger,
            persist_group=active.store.record_group,
        )
        active.store.record_state(active.st, "turn_finished")
    except (llm.LLMError, SessionError, OSError) as exc:
        ui.error(f"Fatal runtime error: {exc}")
        return 1

    print(f"\n[final] {final}")
    print(
        f"[runtime] changed={sorted(active.st.files)} revision={active.st.rev} "
        f"verified_revision={active.st.ok_rev} tokens={active.st.in_tok + active.st.out_tok}"
    )
    print(f"[session] {active.store.path}")
    print(f"[trajectory] {logger.path}")
    return 0


def _show_sessions() -> None:
    summaries = SessionStore.summaries(config.WORKSPACE_DIR)
    if not summaries:
        print("No sessions found for this workspace.")
        return
    for item in summaries:
        print(
            f"{item['id']}  {item['updated']:%Y-%m-%d %H:%M}  "
            f"tasks={item['tasks']}  {item['task']}"
        )


def _show_checkpoints(active: ActiveSession) -> None:
    records = active.st.checkpoints.active()
    if not records:
        print("No restorable Agent file checkpoints.")
        return
    for record in reversed(records):
        action = "created" if not record.get("existed") else "modified"
        print(f"{record['id']}  {action}  {record['path']}")


def _restore_checkpoint(active: ActiveSession, checkpoint_id: str | None) -> None:
    try:
        result = active.st.checkpoints.restore(checkpoint_id)
    except CheckpointError as exc:
        ui.warning(str(exc))
        return
    active.st.rev += 1
    active.st.ok_rev = -1
    active.st.changed = True
    active.st.files.add(result.path)
    active.store.record_state(active.st, f"restored_checkpoint:{result.checkpoint_id}")
    action = "deleted Agent-created file" if result.deleted_created_file else "restored before-image"
    ui.success(
        f"{result.checkpoint_id}: {action} for {result.path}; "
        f"workspace revision is now {active.st.rev}."
    )


def _interactive_loop(logger, active: ActiveSession | None = None) -> int:
    print("Interactive coding-agent session. Type /help for commands, /exit to quit.")
    pending_shell: list[str] = []
    while True:
        try:
            raw = input("agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSession closed.")
            return 0
        if not raw:
            continue

        if raw.startswith("/"):
            command, _, argument = raw.partition(" ")
            command = command.lower()
            argument = argument.strip()
            if command in {"/exit", "/quit"}:
                return 0
            if command == "/help":
                print(interactive.HELP)
            elif command == "/status":
                if active is None:
                    print("No active session. Enter a task or use /resume.")
                else:
                    print(
                        interactive.status_text(
                            active.st,
                            active.store.path,
                            len(active.st.checkpoints.active()),
                        )
                    )
            elif command == "/sessions":
                _show_sessions()
            elif command == "/resume":
                try:
                    active = _resume_active(argument or "latest", logger)
                    pending_shell = []
                    ui.success(f"Resumed session {active.store.session_id}.")
                except (SessionError, OSError, RuntimeError) as exc:
                    ui.warning(str(exc))
            elif command in {"/new", "/clear"}:
                active = None
                pending_shell = []
                print("Context cleared. The next task will start a new session.")
            elif command == "/checkpoints":
                if active is None:
                    print("No active session.")
                else:
                    _show_checkpoints(active)
            elif command == "/undo":
                if active is None:
                    print("No active session.")
                else:
                    _restore_checkpoint(active, None)
            elif command == "/restore":
                if active is None:
                    print("No active session.")
                elif not argument:
                    print("Usage: /restore <checkpoint-id>")
                else:
                    _restore_checkpoint(active, argument)
            else:
                print(f"Unknown command {command}. Type /help.")
            continue

        if raw.startswith("!"):
            cmd = raw[1:].strip()
            result = tools.run_user_command(cmd)
            print(result.text)
            observation = interactive.shell_observation(cmd, result)
            if active is not None:
                active.st.note_user_shell()
                active.store.record_state(active.st, "user_shell_command")
            try:
                include = input("Include this command and output in model context? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                include = "n"
            if include not in {"n", "no"}:
                if active is None:
                    pending_shell.append(observation)
                else:
                    group = [{"role": "user", "content": observation}]
                    active.ctx.add_between_turn_group(group)
                    active.store.record_between_turn(group, active.st)
            continue

        task, references = interactive.expand_file_references(raw, config.WORKSPACE_DIR)
        if pending_shell:
            task += "\n\n" + "\n\n".join(pending_shell)
            pending_shell = []
        try:
            if active is None:
                active = _new_active(task, logger, references)
            else:
                _start_followup(active, task, logger, references)
        except (SessionError, OSError, RuntimeError) as exc:
            ui.error(f"Could not start task: {exc}")
            continue
        _run_active(active, logger)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A small local tool-calling coding agent.")
    parser.add_argument(
        "task",
        nargs="*",
        help="One-shot programming task; omit it to enter interactive mode.",
    )
    parser.add_argument("--yes", action="store_true", help="Disable effect-tool confirmation for this process.")
    parser.add_argument("--workspace", help="Workspace directory for this run.")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        help="Resume the latest session or a session id/filename.",
    )
    parser.add_argument("--list-sessions", action="store_true", help="List resumable sessions and exit.")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.workspace:
        config.WORKSPACE_DIR = str(Path(args.workspace).expanduser().resolve())
    if args.yes:
        config.REQUIRE_CONFIRMATION = False
        ui.warning("Confirmation disabled for this process.")

    if getattr(args, "list_sessions", False) is True:
        _show_sessions()
        return 0

    try:
        config.get_api_key()
        config.get_model()
    except RuntimeError as exc:
        ui.error(f"Configuration error: {exc}")
        return 2

    logger = RunLog()
    active = None
    resume_selector = getattr(args, "resume", None)
    if not isinstance(resume_selector, str):
        resume_selector = None
    if resume_selector:
        try:
            active = _resume_active(resume_selector, logger)
        except (SessionError, OSError, RuntimeError) as exc:
            ui.error(f"Could not resume session: {exc}")
            return 2

    task = " ".join(args.task).strip() if args.task else ""
    if not task:
        return _interactive_loop(logger, active)

    task, references = interactive.expand_file_references(task, config.WORKSPACE_DIR)
    try:
        if active is None:
            active = _new_active(task, logger, references)
        else:
            _start_followup(active, task, logger, references)
    except (SessionError, OSError, RuntimeError) as exc:
        ui.error(f"Could not start task: {exc}")
        return 2
    return _run_active(active, logger)


if __name__ == "__main__":
    raise SystemExit(main())
