"""
Terminal display helpers for Mythos.

Kali Linux-inspired color scheme with priority-based visual indicators.
Green/red dominant palette, safety-aware tool display, hacker aesthetic.
"""

from __future__ import annotations

import os
import re

# Theme primitives — colors, display constants, safety icons, _truncate.
# Re-exported so that callers using `from alpha.display.core import C, c, ...`
# continue to work after the split (Plano-Upgrade-v3 §1.1).
from .theme import (  # noqa: F401
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_MAX_LINES,
    DISPLAY_PREVIEW_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    ELBOW,
    NO_COLOR,
    RESULT_CONT,
    RESULT_INDENT,
    C,
    _pct_color,
    _truncate,
    c,
    fit,
    rel_path,
    supports_color,
    term_cols,
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
    _approve_all,
    _print_plan_card,
    is_auto_accept,
    print_approval_request,
    reset_approve_all,
    set_auto_accept,
    toggle_auto_accept,
)


def print_phase(detail: str) -> None:
    """Display a phase/progress update."""
    print(f"  {c(C.GRAY_DARK, '·')} {c(C.GRAY, detail)}")


def print_error(message: str) -> None:
    """Display an error message in red with border."""
    print(f"\n{c(C.RED, '●')} {c(C.RED + C.BOLD, 'Error:')} {c(C.RED, message)}")


# Last turn summary — surfaced in the bottom_toolbar so it stays visible
# even when a noisy turn pushes the scrolled summary off the viewport.
# Cleared at the start of the next turn.
_last_turn_summary: str | None = None


def _format_turn_summary_line(
    tools_count: int,
    duration_s: float,
    files_touched: set[str] | list[str] | None,
    errors_count: int,
    anomaly: dict | None,
) -> str:
    """One-line ANSI-colored summary (no trailing newline). Same shape used
    for both the inline `print` and the bottom_toolbar pin."""
    parts: list[str] = []
    parts.append(_format_duration(duration_s) or f"{duration_s:.1f}s")
    parts.append(f"{tools_count} tool" + ("s" if tools_count != 1 else ""))
    if files_touched:
        paths = sorted(files_touched)
        labels = [os.path.basename(p) or p for p in paths[:3]]
        extra = len(paths) - 3
        files_label = ", ".join(labels) + (f" (+{extra} mais)" if extra > 0 else "")
        parts.append(files_label)
    if errors_count:
        parts.append(c(C.RED, f"{errors_count} falha" + ("s" if errors_count != 1 else "")))

    icon_color = C.YELLOW if anomaly else (C.RED if errors_count else C.GRAY_DARK)
    icon = "⚠" if (anomaly or errors_count) else "✻"
    return f"{c(icon_color, icon)} {c(C.GRAY_DARK, ' · '.join(parts))}"


def clear_last_turn_summary() -> None:
    """Drop the pinned summary — called at the start of each new turn so the
    bottom_toolbar doesn't show stale info next to a fresh prompt."""
    global _last_turn_summary
    _last_turn_summary = None


def print_turn_summary(
    tools_count: int,
    duration_s: float,
    files_touched: set[str] | list[str] | None = None,
    errors_count: int = 0,
    anomaly: dict | None = None,
) -> None:
    """End-of-turn marker shown after every turn — tools used, time, files,
    plus a yellow/red warning line when the agent stopped abnormally
    (loop detected, max iterations reached, etc.). Also stores the summary
    for the bottom_toolbar so a noisy turn doesn't push it off-screen.

    `anomaly`, when present, is a dict with at least `reason` and `detail`
    keys (matches the agent's `{type: 'anomaly', ...}` event payload).
    """
    global _last_turn_summary
    if anomaly:
        reason = anomaly.get("reason") or "unknown"
        detail = anomaly.get("detail") or reason
        print(f"\n{c(C.YELLOW, '●')} {c(C.YELLOW, f'Agente parou: {detail}')}")

    # CODE_REVIEW_FASE1 #10: a turn that produced no tools, no errors,
    # no files, and no anomaly is a SILENT turn — render the explicit
    # "(turno encerrado)" hint instead of a green ✓ that reads as
    # success. The pinned summary follows the same line so the bottom
    # toolbar shows it too.
    if tools_count == 0 and errors_count == 0 and not files_touched and not anomaly:
        # Plain chat reply: nothing to summarize, and a marker line after
        # every answer is noise. Keep the transcript clean.
        _last_turn_summary = None
        return

    line = _format_turn_summary_line(tools_count, duration_s, files_touched, errors_count, anomaly)
    print(f"\n{line}")
    _last_turn_summary = line


# Lines shown by `print_thinking` in "compact" mode before folding the rest.
THINKING_COMPACT_LINES = 4


def print_thinking(text: str, mode: str = "compact") -> None:
    """Render a block of model reasoning (extended thinking / DeepSeek
    `reasoning_content`) dimmed under a `∴ Pensando` header.

    `mode`: ``compact`` keeps the first `THINKING_COMPACT_LINES` lines and
    folds the rest into `(+N linhas)`; ``full`` prints everything; ``off``
    prints nothing. The text is never passed through the markdown renderer
    — reasoning is prose for the eye, not for formatting.
    """
    if mode == "off":
        return
    body = (text or "").strip()
    if not body:
        return
    # Reflow to the terminal width with a hanging indent: a line the
    # terminal wraps by itself restarts at column 0 and the block stops
    # reading as one indented aside. Blank lines between reasoning
    # paragraphs only stretch the block, so they are dropped.
    import textwrap

    width = max(20, term_cols() - 3)
    lines: list[str] = []
    for raw in body.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if len(raw) > DISPLAY_LINE_TRUNCATE:
            raw = raw[: DISPLAY_LINE_TRUNCATE - 1] + "…"
        lines.extend(textwrap.wrap(raw, width=width) or [raw])
    hidden = 0
    if mode != "full" and len(lines) > THINKING_COMPACT_LINES:
        hidden = len(lines) - THINKING_COMPACT_LINES
        lines = lines[:THINKING_COMPACT_LINES]
    out = [f"\n{c(C.GRAY_DARK, '∴')} {c(C.GRAY + C.ITALIC, 'Pensando…')}"]
    for ln in lines:
        out.append(f"  {c(C.GRAY_DARK + C.ITALIC, ln)}")
    if hidden:
        out.append(
            f"  {c(C.GRAY_DARK, f'… (+{hidden} linhas — ALPHA_SHOW_THINKING=full para ver tudo)')}"
        )
    print("\n".join(out))


def format_assistant_text(rendered: str) -> str:
    """Prefix a rendered reply with the `●` bullet and hang every following
    line two columns in, so assistant prose, tool calls and results share
    one left edge (`●` at column 0, content at column 2)."""
    body = rendered.strip("\n")
    if not body:
        return ""
    # Wrap here, not in the terminal: a row the terminal wraps by itself
    # restarts at column 0 and loses the hanging indent. Table rows are
    # left alone (they are already fitted to the width).
    from .markdown import visible_len, wrap_ansi

    width = term_cols() - 3
    lines: list[str] = []
    for ln in body.split("\n"):
        if "│" in ln and visible_len(ln) > 0:
            lines.append(ln)
        else:
            lines.extend(wrap_ansi(ln, width))
    out = [f"{c(C.WHITE, '●')} {lines[0]}"]
    out.extend(f"  {ln}" if ln else "" for ln in lines[1:])
    return "\n" + "\n".join(out) + "\n"


def format_user_echo(text: str, note: str = "") -> str:
    """The user's message as it stays in the transcript: `❯ text` on a
    subtle gray band, so each turn's start is easy to find when scrolling
    back. `note` is a dim suffix (e.g. `(da fila)`)."""
    import textwrap

    width = max(20, term_cols() - 5)
    lines: list[str] = []
    for raw in (text or "").split("\n"):
        lines.extend(textwrap.wrap(raw, width=width) or [""])
    out = []
    for i, ln in enumerate(lines):
        lead = "❯ " if i == 0 else "  "
        row = f" {lead}{ln} "
        if NO_COLOR:
            out.append(row.rstrip())
        else:
            out.append(f"{C.BG_GRAY}{C.WHITE}{row}{C.RESET}")
    if note:
        out[-1] += f" {c(C.GRAY_DARK, note)}"
    return "\n" + "\n".join(out)


def print_context_compressed(before: int, after: int) -> None:
    """Display context compression event with stats."""
    saved = before - after
    pct = (saved / before * 100) if before > 0 else 0
    print(
        f"\n{c(C.BLUE, '●')} {c(C.GRAY, 'Context compressed:')} "
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
    return c(_pct_color(pct), f"[ctx {int(pct)}%] ")


def print_context_warning(pct: int, used: int, limit: int) -> None:
    """One-line warning when crossing a context-usage threshold.

    Called at most once per threshold per session (50/70/90). Compression
    fires automatically at 70%, so 70% acts as `imminent` and 90% as
    `compressing every turn`.
    """
    color = _pct_color(pct, low=C.YELLOW)
    if pct >= 90:
        icon, label = "⚠", "CRITICAL"
        note = "compactacao acontecendo a cada turno"
    elif pct >= 70:
        icon, label = "⚠", "HIGH"
        note = "compactacao iminente (threshold 70%)"
    else:
        icon, label = "ⓘ", "INFO"
        note = "metade do contexto consumida"
    print(
        f"  {c(color, icon)} {c(color, label)} "
        f"{c(C.GRAY, f'context: {used:,}/{limit:,} tokens ({pct}%)')} "
        f"{c(C.DIM, '— ' + note)}"
    )


# Per-agent state for collapsing consecutive identical tool-call lines into
# `(×N)` in the full trace (ALPHA_SUBAGENT_TRACE=full).
_subagent_last_call: dict[str, dict] = {}

# Compact trace (default): per-agent tool-use counters. Sub-agent calls are
# NOT printed one per line — three parallel agents reading a codebase put
# 50+ interleaved rows in the transcript and bury everything above. The
# spinner carries the live activity instead and the batch result prints one
# `Done <task> · N steps` block per agent.
_subagent_activity: dict[str, int] = {}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def subagent_trace_full() -> bool:
    """True when every sub-agent tool call should be printed on its own row."""
    return os.getenv("ALPHA_SUBAGENT_TRACE", "").strip().lower() in ("full", "1", "on")


def flush_subagent_dup(label_key: str) -> None:
    """Close an agent's live trace: in full mode, append the pending `×N`
    run-length row; in compact mode, drop its counter from the spinner."""
    _subagent_activity.pop(label_key, None)
    state = _subagent_last_call.get(label_key)
    if state and state["count"] > 1:
        print(f"{RESULT_CONT}    {c(C.GRAY_DARK, ELBOW + ' ×' + str(state['count']))}")
    _subagent_last_call.pop(label_key, None)


def _subagent_live_label(agent_label: str, header: str) -> str:
    """Spinner text for the compact trace:
    `3 agents · 47 tool uses · #2 Read(worktimer/db.py)`."""
    plain = _ANSI_RE.sub("", header).lstrip("● ").strip()
    uses = sum(_subagent_activity.values())
    parts = []
    if len(_subagent_activity) > 1:
        parts.append(f"{len(_subagent_activity)} agents")
    parts.append(f"{uses} tool use{'s' if uses != 1 else ''}")
    parts.append(f"{agent_label} {plain}".strip())
    return fit(" · ".join(parts), used=44)


def print_subagent_event(event: dict, agent_label: str = "") -> None:
    """Surface a sub-agent event under the parent's `● Task(…)` header.

    Compact (default): nothing is printed per call — the spinner shows the
    running tool-use count and the latest call. Full
    (`ALPHA_SUBAGENT_TRACE=full`): one indented `⚤ #N ● Name(args)` row per
    call, consecutive identical rows folded into `×N`.
    """
    label_key = agent_label or "_"
    event_type = event.get("type", "")

    if event_type == "tool_call" and not subagent_trace_full():
        header = _format_tool_call_header(
            event.get("name", ""),
            event.get("args", {}),
            event.get("safety", "safe"),
        )
        _subagent_activity[label_key] = _subagent_activity.get(label_key, 0) + 1
        from .thinking import get_active_indicator

        ind = get_active_indicator()
        if ind is not None:
            ind.update_label(_subagent_live_label(agent_label, header))
        return

    gutter = c(C.MAGENTA, "⚤")
    label_str = c(C.MAGENTA + C.DIM, agent_label) if agent_label else ""

    if event_type == "tool_call":
        header = _format_tool_call_header(
            event.get("name", ""),
            event.get("args", {}),
            event.get("safety", "safe"),
        )
        line = (
            f"{RESULT_CONT}{gutter} {label_str} {header}"
            if label_str
            else f"{RESULT_CONT}{gutter} {header}"
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
        _print_result_body(reply.strip().split("\n"), indent=RESULT_CONT + "  ")


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

    # Deferred (lazy-loaded) tools: schema reaches the model only after a
    # `tool_search`. Flag them so `/tools` explains why a call may need a
    # search first.
    from alpha import tool_lazy
    lazy_on = tool_lazy.enabled()
    loaded = tool_lazy.loaded_names()

    # Display grouped
    for cat in sorted(categories.keys()):
        icon = _CATEGORY_ICONS.get(cat, "◆ ")
        print(f"\n  {c(C.GREEN + C.BOLD, f'{icon} {cat.upper()}')} {c(C.GRAY_DARK, '─' * 30)}")
        for fn in sorted(categories[cat], key=lambda f: f.get("name", "")):
            name = fn.get("name", "")
            desc = fn.get("description", "")[:55]
            tag = ""
            if lazy_on and tool_lazy.is_deferred(name, get_tool(name)):
                tag = c(C.GRAY_DARK, " (loaded)" if name in loaded else " (deferred)")
            print(f"    {c(C.CYAN, name):38s} {c(C.GRAY, desc)}{tag}")

    total = sum(len(v) for v in categories.values())
    print(f"\n  {c(C.GRAY, f'{total} tools in {len(categories)} categories')}")


def print_banner(provider: str, model: str) -> None:
    """Mythos startup card: a compact rounded box with the product mark,
    version, model and working directory — everything else is one `/help` away."""
    from .. import __version__

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home or cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]

    rows = [
        (f"✻ Mythos v{__version__} — Vulnerability Hunter",
         f"{c(C.VIOLET_GLOW, '✻')} {c(C.BOLD, 'Mythos')} {c(C.GRAY, f'v{__version__} — Vulnerability Hunter')}"),
        ("", ""),
        (f"  {model} · {provider}", f"  {c(C.GRAY, f'{model} · {provider}')}"),
    ]
    width = min(max(len(rows[0][0]), len(rows[2][0]), len(cwd) + 2) + 2, max(30, term_cols() - 4))
    cwd_plain = "  " + _truncate(cwd, width - 4)
    rows.append((cwd_plain, f"  {c(C.GRAY, cwd_plain[2:])}"))

    edge = C.VIOLET_DARK
    print()
    print(c(edge, "╭" + "─" * (width + 2) + "╮"))
    for plain, styled in rows:
        plain = _truncate(plain, width)
        pad = " " * max(0, width - len(plain))
        print(f"{c(edge, '│')} {styled if len(plain) <= width else plain}{pad} {c(edge, '│')}")
    print(c(edge, "╰" + "─" * (width + 2) + "╯"))
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


