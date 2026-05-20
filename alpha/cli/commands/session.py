"""Session lifecycle handlers: /exit, /clear, /save, /load, /continue, /sessions, /history."""

from __future__ import annotations

import os

from alpha.cost import reset_session as reset_cost_session
from alpha.display import (
    C,
    c,
    print_banner,
    print_sessions_list,
    reset_approve_all,
)
from alpha.history import (
    generate_session_id,
    get_last_session_id,
    list_sessions,
    load_session,
    load_session_summary,
    save_session,
)
from alpha.stats import reset_session as reset_stats_session

from ._types import DispatchResult, ReplContext


def _handle_exit(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    if len(ctx.messages) > 1:
        save_session(
            ctx.session_id,
            ctx.messages,
            {"provider": ctx.provider, "model": ctx.cfg["model"]},
        )
        print(f"  {c(C.GRAY, f'Session saved: {ctx.session_id}')}")
    print(c(C.GRAY, "Goodbye."))
    return DispatchResult.BREAK


def _handle_clear(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    ctx.history.clear()
    ctx.messages[:] = [{"role": "system", "content": ctx.system_prompt}]
    ctx.session_id = generate_session_id()
    reset_approve_all()
    reset_cost_session()
    reset_stats_session()
    os.system("clear" if os.name != "nt" else "cls")
    print_banner(ctx.provider, ctx.cfg["model"])
    return DispatchResult.CONTINUE


def _handle_history(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    if not ctx.history:
        print(c(C.GRAY, "  History is empty."))
    else:
        for msg in ctx.history[-20:]:
            role = msg["role"]
            content = msg["content"][:100]
            color = C.GREEN if role == "user" else C.CYAN
            print(f"  {c(color, role)}: {content}")
    return DispatchResult.CONTINUE


def _handle_save(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    save_session(
        ctx.session_id,
        ctx.messages,
        {"provider": ctx.provider, "model": ctx.cfg["model"]},
    )
    print(f"  {c(C.GREEN, f'Session saved: {ctx.session_id}')}")
    return DispatchResult.CONTINUE


def _apply_loaded_messages(ctx: ReplContext, loaded: list[dict]) -> None:
    """Replace ctx.messages/history with loaded session content."""
    ctx.messages[:] = [{"role": "system", "content": ctx.system_prompt}]
    ctx.messages.extend(loaded)
    ctx.history.clear()
    ctx.history.extend(m for m in loaded if m["role"] in ("user", "assistant"))


def _handle_load(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    if len(parts) < 2:
        sessions = list_sessions(10)
        if not sessions:
            print(c(C.GRAY, "  No saved sessions."))
        else:
            print(f"  {c(C.CYAN, 'Recent sessions:')}")
            for s in sessions:
                print(
                    f"  {c(C.GREEN, s['session_id'])} "
                    f"({s['message_count']} msgs) "
                    f"{c(C.GRAY, s['preview'])}"
                )
            print(f"\n  {c(C.GRAY, 'Usage: /load <session_id>')}")
        return DispatchResult.CONTINUE

    loaded = load_session(parts[1])
    if loaded is None:
        print(c(C.RED, f"  Session not found: {parts[1]}"))
        return DispatchResult.CONTINUE

    _apply_loaded_messages(ctx, loaded)

    # Default: new session_id so `/save` later doesn't overwrite the
    # source. `--inplace` opts into editing the original (#DL018).
    if len(parts) >= 3 and parts[2] == "--inplace":
        ctx.session_id = parts[1]
        print(
            f"  {c(C.GREEN, f'Loaded {len(loaded)} messages from {parts[1]} (in-place: saves overwrite)')}"
        )
    else:
        ctx.session_id = generate_session_id()
        print(
            f"  {c(C.GREEN, f'Loaded {len(loaded)} messages from {parts[1]} into new session {ctx.session_id}')}"
        )
        print(
            f"  {c(C.GRAY, '  (use /load <id> --inplace to overwrite the original instead)')}"
        )
    return DispatchResult.CONTINUE


def _handle_continue(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Resume the last session — delegates to /load with last session ID.

    When a summary is available, wraps it in a context preamble instead
    of loading all messages (avoids blowing the context window).
    """
    last_id = get_last_session_id()
    if not last_id:
        print(c(C.GRAY, "  No previous session found."))
        return DispatchResult.CONTINUE

    summary = load_session_summary(last_id)
    if not summary:
        # No summary available — delegate directly to /load logic.
        return _handle_load(ctx, ["/load", last_id])

    # Summary available — inject as context preamble in new session.
    ctx.messages[:] = [{"role": "system", "content": ctx.system_prompt}]
    ctx.messages.append(
        {
            "role": "user",
            "content": (
                f"[CONTEXT FROM PREVIOUS SESSION {last_id}]\n\n"
                f"{summary}\n\n"
                "[End of previous context. Continue from here.]"
            ),
        }
    )
    ctx.messages.append(
        {
            "role": "assistant",
            "content": (
                "Understood. I have the context from our previous session. "
                "How would you like to continue?"
            ),
        }
    )
    ctx.history.clear()
    print(f"  {c(C.GREEN, f'Resumed with summary from {last_id}')}")
    ctx.session_id = generate_session_id()
    return DispatchResult.CONTINUE


def _handle_sessions(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    print_sessions_list(list_sessions(20))
    return DispatchResult.CONTINUE
