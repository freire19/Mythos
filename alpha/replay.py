"""
Session replay — re-run a saved conversation against any provider
(Plano-Upgrade-v3 H2 #9 phase 2).

Takes a session ID from `~/.alpha/sessions/`, extracts the user turns
in order, and re-feeds them into a fresh agent loop with the chosen
provider. Prints the original assistant reply alongside the new one
for each turn so you can eyeball cross-provider drift, regression-test
prompt changes, or confirm a refactor didn't alter behavior.

Usage:
    python -m alpha.replay <session-id> [--provider anthropic] [--diff]

The fixture-driven test replay (`alpha.llm_fixtures.build_replay_stream`)
is unrelated — that one swaps in canned LLM events for unit tests. This
script runs the REAL agent loop against a real provider.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import logging
import sys
from typing import Any

from .agent import run_agent
from .display import C, c, render_markdown
from .history import load_session
from .tools import get_openai_tools, get_tool

logger = logging.getLogger(__name__)


def extract_user_turns(messages: list[dict]) -> list[str]:
    """Pull the user-side prompts out of a saved messages array in order."""
    out: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            # Multimodal user messages: concatenate text blocks; ignore images.
            chunks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if chunks:
                out.append("\n".join(chunks))
    return out


def extract_assistant_replies(messages: list[dict]) -> list[str]:
    """Pull the FINAL assistant text after each user turn.

    Output length always matches `extract_user_turns(messages)` length so
    callers can pair them by index. An empty string represents a user
    turn that ended without a plain-text assistant reply (e.g. only
    tool calls happened)."""
    out: list[str] = []
    last_assistant_text = ""
    seen_user = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            if seen_user:
                out.append(last_assistant_text)
            seen_user = True
            last_assistant_text = ""
        elif role == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                last_assistant_text = content
    if seen_user:
        out.append(last_assistant_text)
    return out


async def _capture_reply(
    messages: list[dict],
    user_message: str,
    provider: str,
) -> str:
    """Drive run_agent for one turn and return the assembled assistant text."""
    captured: list[str] = []
    async for event in run_agent(
        messages=messages,
        user_message=user_message,
        provider=provider,
        get_tool_fn=get_tool,
        tools=get_openai_tools(),
    ):
        t = event.get("type")
        if t == "token":
            captured.append(event.get("text", ""))
        elif t == "done":
            reply = event.get("reply")
            if isinstance(reply, str) and reply:
                return reply
        elif t == "error":
            return f"[error: {event.get('message', 'unknown')}]"
    return "".join(captured)


def _render_diff(original: str, replay: str) -> str:
    """Unified diff between original and replayed reply, colorized."""
    lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=False),
            replay.splitlines(keepends=False),
            fromfile="original",
            tofile="replay",
            lineterm="",
        )
    )
    if not lines:
        return c(C.GRAY, "  (identical)")
    out: list[str] = []
    for ln in lines:
        if ln.startswith("+") and not ln.startswith("+++"):
            out.append(c(C.GREEN, ln))
        elif ln.startswith("-") and not ln.startswith("---"):
            out.append(c(C.RED, ln))
        elif ln.startswith("@@"):
            out.append(c(C.CYAN, ln))
        else:
            out.append(c(C.GRAY, ln))
    return "\n".join(out)


async def replay_session(
    session_id: str,
    provider: str,
    show_diff: bool = False,
) -> dict:
    """Replay every user turn from `session_id` against `provider`.

    Returns a structured dict so callers (CLI, tests) can post-process.
    Prints progress as it goes — replay is slow because it issues real
    LLM calls."""
    saved = load_session(session_id)
    if saved is None:
        return {"ok": False, "error": f"session not found: {session_id}"}

    user_turns = extract_user_turns(saved)
    originals = extract_assistant_replies(saved)
    if not user_turns:
        return {"ok": False, "error": "session has no user turns"}

    # Align lengths (extract_assistant_replies always returns len(user_turns) entries)
    originals = (originals + [""] * len(user_turns))[: len(user_turns)]

    # Pick up the system prompt the same way main.py does. Skip it here
    # if absent — run_agent tolerates messages without a system entry.
    sys_msg = next(
        (m for m in saved if isinstance(m, dict) and m.get("role") == "system"),
        None,
    )
    replay_messages: list[dict] = [sys_msg] if sys_msg else []

    turns_out: list[dict] = []
    for i, user_text in enumerate(user_turns):
        print(c(C.VIOLET + C.BOLD, f"\n── Turn {i + 1}/{len(user_turns)} ──"))
        print(c(C.GRAY, f"user: {user_text[:160]}" + ("…" if len(user_text) > 160 else "")))

        replay_messages.append({"role": "user", "content": user_text})
        replay_reply = await _capture_reply(replay_messages, user_text, provider)
        replay_messages.append({"role": "assistant", "content": replay_reply})

        orig_reply = originals[i]
        print(c(C.GREEN, "── replay reply ──"))
        print(render_markdown(replay_reply) if replay_reply else c(C.GRAY, "(empty)"))

        if show_diff:
            print(c(C.YELLOW, "── diff vs original ──"))
            print(_render_diff(orig_reply, replay_reply))

        turns_out.append({
            "turn": i + 1,
            "user": user_text,
            "original": orig_reply,
            "replay": replay_reply,
        })

    return {
        "ok": True,
        "session_id": session_id,
        "provider": provider,
        "n_turns": len(user_turns),
        "turns": turns_out,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alpha.replay",
        description=(
            "Replay a saved session against any provider. Prints original "
            "vs new assistant replies turn-by-turn."
        ),
    )
    parser.add_argument("session_id", help="session id from ~/.alpha/sessions/")
    parser.add_argument(
        "--provider",
        default="deepseek",
        help="provider to replay against (default: deepseek)",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="show a unified diff between original and replay per turn",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING)
    result = asyncio.run(replay_session(args.session_id, args.provider, args.diff))
    if not result.get("ok"):
        print(c(C.RED, f"error: {result.get('error', 'unknown')}"), file=sys.stderr)
        return 1
    print(c(C.GREEN + C.BOLD, f"\n✓ replayed {result['n_turns']} turn(s) on {result['provider']}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
