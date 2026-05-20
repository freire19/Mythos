"""
Terminal display helpers for Alpha Code.

Kali Linux-inspired color scheme with priority-based visual indicators.
Green/red dominant palette, safety-aware tool display, hacker aesthetic.
"""

from __future__ import annotations

import os

# Theme primitives — colors, display constants, safety icons, _truncate.
# Re-exported so that callers using `from alpha.display.core import C, c, ...`
# continue to work after the split (Plano-Upgrade-v3 §1.1).
from .theme import (  # noqa: F401
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_MAX_LINES,
    DISPLAY_PREVIEW_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    NO_COLOR,
    C,
    _truncate,
    c,
    supports_color,
)


# Markdown rendering moved to `alpha.display.markdown`. Re-exported here
# so the existing `from alpha.display.core import render_markdown` path
# keeps working (Plano-Upgrade-v3 §1.1).
from .markdown import render_markdown  # noqa: F401, E402

# Tool call rendering moved to `alpha.display.renderers.tools`. Re-exported
# for back-compat (Plano-Upgrade-v3 §1.1).
from .renderers.planning import _TODO_STATUS_GLYPH, _print_todo_list  # noqa: F401
from .renderers.tools import (  # noqa: F401
    _CATEGORY_ICONS,
    _display_tool_name,
    _format_tool_call_header,
    _print_result_body,
    _render_diff,
    _tool_args_preview,
    label_for_tool,
    live_label_for_tool,
    print_tool_call,
    print_tool_result,
)


# Approval prompts + auto-accept state moved to `alpha.display.renderers.prompts`.
# Re-exported for back-compat (Plano-Upgrade-v3 §1.1).
from .renderers.prompts import (  # noqa: F401, E402
    _AUTO_ACCEPT_SETTING_KEY,
    _approve_all,
    _auto_accept_settings_path,
    _load_auto_accept_default,
    _persist_auto_accept,
    _print_plan_card,
    is_auto_accept,
    print_approval_request,
    reset_approve_all,
    set_auto_accept,
    toggle_auto_accept,
)


def print_phase(detail: str) -> None:
    """Display a phase/progress update."""
    print(f"  {c(C.VIOLET_DARK, '→')} {c(C.DIM, detail)}")


def print_error(message: str) -> None:
    """Display an error message in red with border."""
    print(f"\n  {c(C.RED + C.BOLD, '✗ Error:')} {c(C.RED, message)}")


def print_silent_turn() -> None:
    """Marker for turns that produced no visible output — keeps the user
    from staring at a bare prompt and wondering whether the agent froze."""
    print(f"  {c(C.GRAY_DARK, '·')} {c(C.GRAY, '(turno encerrado — envie próxima instrução)')}")


def print_context_compressed(before: int, after: int) -> None:
    """Display context compression event with stats."""
    saved = before - after
    pct = (saved / before * 100) if before > 0 else 0
    print(
        f"  {c(C.BLUE, '⟳')} {c(C.DIM, 'Context compressed:')} "
        f"{c(C.GRAY, str(before))} → {c(C.GREEN, str(after))} tokens "
        f"{c(C.GREEN_DARK, f'(-{pct:.0f}%)')}"
    )


def _context_pct(messages: list[dict], provider: str) -> tuple[int, int, float]:
    """Return (used_tokens, limit_tokens, pct_used)."""
    from ..context import estimate_messages_tokens, get_context_limit

    used = estimate_messages_tokens(messages)
    limit = get_context_limit(provider)
    pct = (used / limit * 100) if limit else 0.0
    return used, limit, pct


def format_context_indicator(messages: list[dict], provider: str) -> str:
    """Compact `[ctx N%]` chip for the REPL prompt. Color shifts with %.

    Returns an empty string when usage is below 1% — keeps the prompt
    clean during light sessions.
    """
    _, _, pct = _context_pct(messages, provider)
    if pct < 1:
        return ""
    if pct >= 90:
        color = C.RED + C.BOLD
    elif pct >= 70:
        color = C.YELLOW + C.BOLD
    elif pct >= 50:
        color = C.YELLOW
    else:
        color = C.GRAY
    return c(color, f"[ctx {int(pct)}%] ")


def print_context_warning(pct: int, used: int, limit: int) -> None:
    """One-line warning when crossing a context-usage threshold.

    Called at most once per threshold per session (50/70/90). Compression
    fires automatically at 70%, so 70% acts as `imminent` and 90% as
    `compressing every turn`.
    """
    if pct >= 90:
        color, icon, label = C.RED + C.BOLD, "⚠", "CRITICAL"
        note = "compactacao acontecendo a cada turno"
    elif pct >= 70:
        color, icon, label = C.YELLOW + C.BOLD, "⚠", "HIGH"
        note = "compactacao iminente (threshold 70%)"
    else:
        color, icon, label = C.YELLOW, "ⓘ", "INFO"
        note = "metade do contexto consumida"
    print(
        f"  {c(color, icon)} {c(color, label)} "
        f"{c(C.GRAY, f'context: {used:,}/{limit:,} tokens ({pct}%)')} "
        f"{c(C.DIM, '— ' + note)}"
    )


# Per-agent state for collapsing consecutive identical tool-call lines into
# `(×N)`. A sub-agent that emits read_file(executor.py) five times in a row
# (loop or retry) otherwise floods the terminal with copies and hides the
# rest of the activity.
_subagent_last_call: dict[str, dict] = {}


def flush_subagent_dup(label_key: str) -> None:
    """If the last call for this agent repeated, append a `(×N)` summary line
    so the user can see the run length without flooding."""
    state = _subagent_last_call.get(label_key)
    if state and state["count"] > 1:
        print(f"     {c(C.GRAY_DARK, '└ ×' + str(state['count']))}")
    _subagent_last_call.pop(label_key, None)


def print_subagent_event(event: dict, agent_label: str = "") -> None:
    """Display a sub-agent event indented one level under the parent.

    Uses the same `● Name(args)` / `└ result` look as the top-level tools,
    just shifted right with `  ⚤` as the agent gutter so the hierarchy
    reads at a glance. Consecutive identical tool-call lines are folded
    into `(×N)` to keep the stream readable when an agent loops.
    """
    gutter = c(C.MAGENTA, "⚤")
    label_str = c(C.MAGENTA + C.DIM, agent_label) if agent_label else ""
    label_key = agent_label or "_"

    event_type = event.get("type", "")
    if event_type == "tool_call":
        header = _format_tool_call_header(
            event.get("name", ""),
            event.get("args", {}),
            event.get("safety", "safe"),
        )
        line = (
            f"  {gutter} {label_str}  {header}"
            if label_str
            else f"  {gutter} {header}"
        )
        state = _subagent_last_call.get(label_key)
        if state and state["line"] == line:
            state["count"] += 1
            return  # suppress the duplicate; counter prints on flush
        flush_subagent_dup(label_key)
        print(line)
        _subagent_last_call[label_key] = {"line": line, "count": 1}
    elif event_type == "done":
        flush_subagent_dup(label_key)
        reply = str(event.get("reply", ""))
        if not reply:
            return
        _print_result_body(reply.strip().split("\n"), indent="    ")


def print_tools_list(tools: list[dict]) -> None:
    """Display tools grouped by category with safety indicators.

    Uses the tool registry for canonical category names, falling back
    to name-prefix inference for unregistered tools (shouldn't happen).
    """
    if not tools:
        print(c(C.GRAY, "  No tools loaded."))
        return

    from alpha.tools import get_tool

    # Group by registry category
    categories: dict[str, list[dict]] = {}
    for t in tools:
        fn = t.get("function", {})
        name = fn.get("name", "")

        # Primary: registry lookup for canonical category
        td = get_tool(name)
        if td and td.category:
            cat = td.category
        else:
            # Fallback: name-prefix inference (shouldn't be needed)
            cat = "general"
            if name.startswith("git_"):
                cat = "git"
            elif name.startswith("execute_shell"):
                cat = "shell"
            elif name.startswith("execute_python") or name.startswith("code_"):
                cat = "code"
            elif name.startswith("http_") or name.startswith("web_") or name.startswith("dns_"):
                cat = "network"
            elif name.startswith("query_") or name.startswith("db_"):
                cat = "database"
            elif name.startswith("delegate_"):
                cat = "agent"
            elif name.startswith("system_") or name.startswith("env_"):
                cat = "system"
            elif name.startswith("browser_"):
                cat = "browser"
            elif name.startswith("search"):
                cat = "search"
            elif name in ("project_overview", "run_tests", "deploy_check", "search_and_replace"):
                cat = "composite"
            elif name in ("read_file", "write_file", "edit_file", "list_directory",
                          "search_files", "glob_files"):
                cat = "filesystem"

        categories.setdefault(cat, []).append(fn)

    # Display grouped
    for cat in sorted(categories.keys()):
        icon = _CATEGORY_ICONS.get(cat, "◆ ")
        print(f"\n  {c(C.GREEN + C.BOLD, f'{icon} {cat.upper()}')} {c(C.GRAY_DARK, '─' * 30)}")
        for fn in sorted(categories[cat], key=lambda f: f.get("name", "")):
            name = fn.get("name", "")
            desc = fn.get("description", "")[:55]
            print(f"    {c(C.CYAN, name):38s} {c(C.GRAY, desc)}")

    total = sum(len(v) for v in categories.values())
    print(f"\n  {c(C.GRAY, f'{total} tools in {len(categories)} categories')}")


def print_banner(provider: str, model: str) -> None:
    """Display the Alpha Code startup banner — Kali Linux inspired."""
    cwd = os.getcwd()

    # Kali-style ASCII banner
    banner = r"""
  ╔══════════════════════════════════════════════════╗
  ║   █████╗ ██╗     ██████╗ ██╗  ██╗ █████╗        ║
  ║  ██╔══██╗██║     ██╔══██╗██║  ██║██╔══██╗       ║
  ║  ███████║██║     ██████╔╝███████║███████║       ║
  ║  ██╔══██║██║     ██╔═══╝ ██╔══██║██╔══██║       ║
  ║  ██║  ██║███████╗██║     ██║  ██║██║  ██║       ║
  ║  ╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝       ║
  ╚══════════════════════════════════════════════════╝"""

    from .. import __version__

    print(c(C.VIOLET + C.BOLD, banner))
    print(
        f"  {c(C.VIOLET_DARK, '│')} {c(C.WHITE + C.BOLD, 'ALPHA CODE')} "
        f"{c(C.VIOLET_GLOW, f'v{__version__}')} {c(C.GRAY, '— Terminal Agent')}"
    )
    print(f"  {c(C.VIOLET_DARK, '│')} {c(C.GRAY, 'cwd:')} {c(C.VIOLET, cwd)}")
    print(f"  {c(C.VIOLET_DARK, '│')} {c(C.GRAY, 'provider:')} {c(C.CYAN, f'{provider} ({model})')}")
    print(f"  {c(C.VIOLET_DARK, '│')} {c(C.GRAY, 'Commands:')} /clear /history /continue /tools /model /help /exit")
    print()


def print_iteration_status(iteration: int, max_iter: int, tokens: int = 0) -> None:
    """Show current iteration and token usage."""
    token_str = f" | {tokens} tokens" if tokens else ""
    print(
        f"  {c(C.GRAY_DARK, '[')} "
        f"{c(C.GREEN_DARK, f'iter {iteration}/{max_iter}')}"
        f"{c(C.GRAY, token_str)} "
        f"{c(C.GRAY_DARK, ']')}"
    )


def print_sessions_list(sessions: list[dict]) -> None:
    """Display saved sessions with formatted output."""
    if not sessions:
        print(c(C.GRAY, "  No saved sessions."))
        return
    for s in sessions:
        sid = c(C.GREEN, s["session_id"])
        ts = c(C.GRAY, s.get("timestamp_human", ""))
        count = c(C.BLUE, f'{s["message_count"]} msgs')
        preview = c(C.DIM, s.get("preview", ""))
        print(f"  {sid} {ts} ({count}) {preview}")


def print_providers_list(
    providers: list[dict],
    *,
    current: str | None = None,
    default: str | None = None,
    numbered: bool = False,
) -> None:
    """Render a provider list with unified formatting.

    numbered=True prefixes rows with `1.`, `2.` (for startup picker).
    current=<id> marks the active provider with a green dot.
    default=<id> appends a gray `(default)` suffix.
    """
    for i, p in enumerate(providers, 1):
        status = c(C.GREEN, "available") if p["available"] else c(C.RED, "no key")
        tag = "" if p["supports_tools"] else c(C.YELLOW, "  chat-only")
        if numbered:
            prefix = f"{c(C.CYAN, str(i))}."
        elif current is not None:
            prefix = c(C.GREEN, "●") if p["id"] == current else " "
        else:
            prefix = " "
        suffix = c(C.GRAY, " (default)") if default and p["id"] == default else ""
        print(f"  {prefix} {c(C.CYAN, p['id']):15s} {p['model']:30s} {status}{tag}{suffix}")



def _format_duration(seconds: float) -> str:
    """Format elapsed seconds as `Xs`, `Xm Ys`, or `Xh Ym`."""
    s = int(seconds)
    if s < 1:
        return ""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h {m}m"


def _format_tokens(n: int) -> str:
    """Format token count with k/M suffix (1234 → 1.2k, 1234567 → 1.2M)."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


_HINT_PHRASES = (
    (8, "warming up"),
    (20, "exploring"),
    (45, "deep in thought"),
    (90, "iterating"),
    (180, "almost done thinking"),
    (360, "still going"),
)


def _hint_for(seconds: float) -> str:
    last = ""
    for threshold, phrase in _HINT_PHRASES:
        if seconds >= threshold:
            last = phrase
        else:
            break
    return last


# label_for_tool / live_label_for_tool now live in renderers.tools
# (imported above at the top of the file).


