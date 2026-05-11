"""
Terminal display helpers for Alpha Code.

Kali Linux-inspired color scheme with priority-based visual indicators.
Green/red dominant palette, safety-aware tool display, hacker aesthetic.
"""

import asyncio
import json
import os
import shutil
import sys
import time

# ─── Display truncation constants (#D010 V1.0) ───
#
# Antes esses limites viviam inline em ~6 funcoes diferentes (`[:200]`,
# `[:120]`, `[:97]+"..."`, `max_lines = 8`). Centralizar em um lugar
# unico permite ajuste consistente e elimina mismatches silenciosos.
DISPLAY_LINE_TRUNCATE = 200       # max chars per terminal line
DISPLAY_PREVIEW_TRUNCATE = 120    # last-reply preview / TUI status
DISPLAY_PROMPT_VALUE_TRUNCATE = 100  # approval prompt arg values (followed by ...)
DISPLAY_MAX_LINES = 8             # max lines from a tool result


# ─── ANSI Colors (Kali Linux palette) ───


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Core palette — solid tones, less neon. Slot 82 (#5fff5f) and 46
    # (#00ff00) vibrate too much on dark terminals; switched to forest
    # 34 (#00af00). Same idea for cyan/yellow: pull saturation back so
    # the violet brand stays the loudest thing on screen.
    GREEN = "\033[38;5;34m"       # #00af00 forest green (solid)
    GREEN_DARK = "\033[38;5;28m"  # #008700 darker shade
    GREEN_NEON = "\033[38;5;46m"  # #00ff00 reserved for thinking pulse only
    RED = "\033[38;5;160m"        # #d70000 solid red (was 196 — neon)
    RED_DARK = "\033[38;5;124m"   # #af0000
    YELLOW = "\033[38;5;178m"     # #d7af00 gold (was 220 — neon yellow)
    ORANGE = "\033[38;5;172m"     # #d78700 (was 208 — neon orange)
    BLUE = "\033[38;5;33m"        # #0087ff
    CYAN = "\033[38;5;37m"        # #00afaf teal (was 51 — neon cyan)
    MAGENTA = "\033[38;5;135m"    # Purple (sub-agents)

    # Refined violet palette — the Alpha brand color. Picked over the
    # pinker MAGENTA(#af5fff) because the blue undertone reads as alive
    # rather than carnival-bright. VIOLET_GLOW is the breathe-peak shade
    # for the thinking-indicator pulse.
    VIOLET = "\033[38;5;99m"        # #875fff (main brand, electric)
    VIOLET_GLOW = "\033[38;5;141m"  # #af87ff (lavender, pulse peak)
    VIOLET_DARK = "\033[38;5;61m"   # #5f5faf (muted indigo, borders)

    # Soft amber for the accept-edits status chip — distinct from the
    # brand violet so the user can spot the mode at a glance, but
    # desaturated enough not to feel like an alert.
    AMBER_SOFT = "\033[38;5;180m"   # #d7af87 (muted gold)
    WHITE = "\033[38;5;255m"      # Bright white
    GRAY = "\033[38;5;245m"       # Medium gray
    GRAY_DARK = "\033[38;5;238m"  # Dark gray (borders)

    # Backgrounds
    BG_RED = "\033[48;5;52m"      # Dark red background
    BG_GREEN = "\033[48;5;22m"    # Dark green background
    BG_YELLOW = "\033[48;5;58m"   # Dark yellow background
    BG_GRAY = "\033[48;5;236m"    # Dark gray background


def supports_color() -> bool:
    """Check if the terminal supports ANSI color codes."""
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


NO_COLOR = not supports_color()


def c(color: str, text: str) -> str:
    """Wrap text in ANSI color codes. Returns plain text if color is unsupported."""
    if NO_COLOR:
        return text
    return f"{color}{text}{C.RESET}"


# ─── Safety color mapping ───

_SAFETY_COLORS = {
    "safe": C.GREEN,
    "destructive": C.RED,
    "unknown": C.YELLOW,
}

_SAFETY_ICONS = {
    "safe": "●",
    "destructive": "⚠",
    "unknown": "?",
}

# Category icons for /tools display.
# Chaves baterem com `td.category` em register_tool. Antes tinha `file`
# (tool usa `filesystem`), `pipeline` (nenhuma tool), e faltava
# `composite/browser/scraping/skills` — metade das categorias caia no
# fallback `◆`. (#DM013)
_CATEGORY_ICONS = {
    "filesystem": "📁",
    "shell": "🖥",
    "code": "⟨⟩",
    "git": "⎇ ",
    "network": "🌐",
    "search": "🔍",
    "database": "🗄",
    "system": "⚙ ",
    "agent": "🤖",
    "browser": "🌍",
    "scraping": "🕷",
    "skills": "📚",
    "composite": "⛓ ",
    "general": "◆ ",
}


# Cosmetic aliases — the LLM still sees the canonical name in tool_calls,
# this only changes the label rendered in the terminal.
_DISPLAY_TOOL_NAME_ALIASES = {
    "execute_shell": "bash",
}


def _display_tool_name(name: str) -> str:
    return _DISPLAY_TOOL_NAME_ALIASES.get(name, name)


# ─── Display functions ───


def _tool_args_preview(args: dict) -> str:
    """Pick the most informative arg and truncate it for the header line."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in ("path", "command", "query", "action", "pattern", "file", "code"):
        if key in args:
            val = str(args[key])
            break
    else:
        val = str(next(iter(args.values())))
    if len(val) > DISPLAY_PREVIEW_TRUNCATE:
        val = val[:DISPLAY_PREVIEW_TRUNCATE - 1] + "…"
    return val.replace("\n", " ")


def _format_tool_call_header(name: str, args: dict, safety: str) -> str:
    """Build `{icon} {ToolName}({preview})` — shared by top-level and sub-agent
    rendering so the two visual variants never drift."""
    safety_color = _SAFETY_COLORS.get(safety, C.YELLOW)
    icon = c(safety_color, _SAFETY_ICONS.get(safety, "⚡"))
    name_color = C.VIOLET if safety == "safe" else safety_color
    tool_name = c(name_color + C.BOLD, _display_tool_name(name))
    preview = _tool_args_preview(args)
    paren = (
        f"{c(C.GRAY_DARK, '(')}{c(C.GRAY, preview)}{c(C.GRAY_DARK, ')')}"
        if preview else ""
    )
    return f"{icon} {tool_name}{paren}"


def print_tool_call(name: str, args: dict, safety: str = "safe") -> None:
    """Display a tool call as `  ● ToolName(args)` — Claude-Code style."""
    print(f"  {_format_tool_call_header(name, args, safety)}")


_TODO_STATUS_GLYPH = {
    "pending": ("☐", C.GRAY),
    "in_progress": ("◐", C.YELLOW),
    "completed": ("☑", C.GREEN),
    "cancelled": ("☒", C.RED_DARK),
}


def _print_todo_list(todos: list) -> None:
    if not todos:
        print(f"  {c(C.GRAY, '(empty todo list)')}")
        return
    for t in todos:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status", "pending"))
        glyph, color = _TODO_STATUS_GLYPH.get(status, ("•", C.GRAY))
        content = str(t.get("content", ""))
        if len(content) > 200:
            content = content[:197] + "..."
        line_color = C.GRAY if status in ("completed", "cancelled") else C.WHITE
        print(f"  {c(color, glyph)} {c(line_color, content)}")




_DIFF_MAX_LINES = 40


def _render_diff(old_text: str, new_text: str, path: str | None = None) -> None:
    """Render a unified diff with green/red highlighted blocks (git-style).

    Lines added are shown on a green background, removed on red, context in
    gray. Output is bounded by `_DIFF_MAX_LINES` to avoid flooding the
    terminal on large rewrites.
    """
    import difflib

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    if path:
        print(f"  {c(C.GRAY_DARK, '┌─')} {c(C.CYAN, path)}")

    diff = list(difflib.unified_diff(old_lines, new_lines, n=2, lineterm=""))
    if not diff:
        print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, '(no textual changes)')}")
        return

    # Skip the file headers (---/+++) since we already printed the path.
    body = [ln for ln in diff if not (ln.startswith("---") or ln.startswith("+++"))]

    shown = 0
    for line in body:
        if shown >= _DIFF_MAX_LINES:
            remaining = len(body) - shown
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, f'... ({remaining} more diff lines)')}")
            break

        if line.startswith("@@"):
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.CYAN + C.DIM, line[:DISPLAY_LINE_TRUNCATE])}")
        elif line.startswith("+"):
            text = line[1:]
            if len(text) > DISPLAY_LINE_TRUNCATE:
                text = text[:DISPLAY_LINE_TRUNCATE - 3] + "..."
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.BG_GREEN + C.WHITE, '+ ' + text)}")
        elif line.startswith("-"):
            text = line[1:]
            if len(text) > DISPLAY_LINE_TRUNCATE:
                text = text[:DISPLAY_LINE_TRUNCATE - 3] + "..."
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.BG_RED + C.WHITE, '- ' + text)}")
        else:
            text = line[1:] if line.startswith(" ") else line
            if len(text) > DISPLAY_LINE_TRUNCATE:
                text = text[:DISPLAY_LINE_TRUNCATE - 3] + "..."
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, '  ' + text)}")
        shown += 1

    print(f"  {c(C.GRAY_DARK, '└─')}")


def _print_result_body(lines: list[str], indent: str = "  ") -> None:
    """Render a result body in Claude-Code style: `  └ first` then aligned
    continuation lines, capped with `  … +N lines (ctrl+o to expand)`."""
    if not lines:
        return
    elbow = c(C.GRAY_DARK, "└")
    cont = " " * (len(indent) + 2)
    shown = min(len(lines), DISPLAY_MAX_LINES)
    for i, line in enumerate(lines[:shown]):
        truncated = line[:DISPLAY_LINE_TRUNCATE]
        if i == 0:
            print(f"{indent}{elbow} {truncated}")
        else:
            print(f"{cont}{truncated}")
    if len(lines) > shown:
        remaining = len(lines) - shown
        print(f"{indent}{c(C.GRAY, f'… +{remaining} lines (ctrl+o to expand)')}")


def _result_summary_line(result: dict) -> str:
    """Pick the most informative key for a result without printable output."""
    short = result.get("path") or str(result.get("count", ""))
    if not short:
        keys = [k for k in result if not k.startswith("_")]
        if keys:
            short = str(result[keys[0]])[:DISPLAY_LINE_TRUNCATE - 20]
    short = str(short)
    if len(short) > DISPLAY_LINE_TRUNCATE - 10:
        short = short[:DISPLAY_LINE_TRUNCATE - 11] + "…"
    return short


_DELEGATE_TASK_MAX_STEPS = 5


def _strike(text: str) -> str:
    """Wrap text in ANSI strikethrough — used for completed sub-agent steps."""
    if NO_COLOR:
        return text
    return f"\033[9m{text}\033[29m"


def _print_delegate_step(step: object) -> None:
    """Render one step inside a Task block: `✔ tool_name: args_preview`."""
    if isinstance(step, dict):
        name = str(step.get("name", "?"))
        preview = str(step.get("args_preview", ""))
        text = f"{name}: {preview}" if preview else name
    else:
        text = str(step)
    if len(text) > DISPLAY_PREVIEW_TRUNCATE:
        text = text[:DISPLAY_PREVIEW_TRUNCATE - 1] + "…"
    check = c(C.GREEN, "✔")
    print(f"        {check} {c(C.GRAY, _strike(text))}")


def _print_delegate_single(task_label: str, result: dict) -> None:
    """Render a single sub-agent result as an inline Task block."""
    steps = result.get("tools_used") or []
    iterations = result.get("iterations", len(steps))
    short = task_label[:DISPLAY_PROMPT_VALUE_TRUNCATE]
    if len(task_label) > DISPLAY_PROMPT_VALUE_TRUNCATE:
        short = short[:-1] + "…"
    suffix = f"({iterations} steps)"
    print(f"  {c(C.VIOLET + C.BOLD, '✪')} {c(C.WHITE + C.BOLD, short)} {c(C.GRAY, suffix)}")
    shown = min(len(steps), _DELEGATE_TASK_MAX_STEPS)
    for step in steps[:shown]:
        _print_delegate_step(step)
    extra = len(steps) - shown
    if extra > 0:
        print(f"        {c(C.GRAY, f'… +{extra} completed')}")


def _print_delegate_aggregate(name: str, result: dict, args: dict | None) -> None:
    """Top-level entry for the `✪ TaskName … (N steps)` view used by
    delegate_task (single block) and delegate_parallel (one block per
    sub-agent)."""
    if name == "delegate_parallel":
        sub_results = result.get("results") or []
        for sub in sub_results:
            if isinstance(sub, dict):
                label = sub.get("task") or f"task-{sub.get('task_index', '?')}"
                _print_delegate_single(str(label), sub)
        return
    task_label = ""
    if isinstance(args, dict):
        task_label = str(args.get("task", ""))
    if not task_label:
        task_label = "sub-agent task"
    _print_delegate_single(task_label, result)


def print_tool_result(name: str, result: dict, args: dict | None = None) -> None:
    """Display a tool result Claude-Code-style: `  └ first line` + aligned
    continuation + `  … +N lines (ctrl+o to expand)` footer."""
    if not isinstance(result, dict):
        _print_result_body([str(result)])
        return

    if name in ("delegate_task", "delegate_parallel") and not result.get("error"):
        _print_delegate_aggregate(name, result, args)
        return

    if result.get("error"):
        msg = str(result["error"])[:DISPLAY_LINE_TRUNCATE]
        print(f"  {c(C.RED, '└')} {c(C.RED, msg)}")
        return

    if result.get("skipped"):
        reason = result.get("reason", "denied")
        print(f"  {c(C.YELLOW, '└')} {c(C.YELLOW, reason[:DISPLAY_LINE_TRUNCATE])}")
        return

    # Lazy import — thinking imports core, so a top-level import would loop.
    if name == "todo_write" and isinstance(result.get("todos"), list):
        todos = result["todos"]
        from .thinking import get_active_indicator, set_pinned_todos
        ind = get_active_indicator()
        if ind is not None and ind._scroll_active:
            set_pinned_todos(todos)
        else:
            _print_todo_list(todos)
        warning = result.get("warning")
        if warning:
            print(f"  {c(C.YELLOW, '⚠')} {c(C.YELLOW, warning)}")
        return

    # Diff rendering for edits — shows full +/- diff card, has its own framing.
    if name == "edit_file" and isinstance(args, dict) and args.get("old_text"):
        _render_diff(
            str(args.get("old_text", "")),
            str(args.get("new_text", "")),
            str(result.get("path") or args.get("path") or "") or None,
        )
        return
    if name == "write_file" and isinstance(args, dict) and args.get("content"):
        _render_diff(
            str(result.get("_previous_content", "")),
            str(args.get("content", "")),
            str(result.get("path") or args.get("path") or "") or None,
        )
        return

    # Compact one-line summary for file ops without diff args.
    if name in ("edit_file", "write_file") and not result.get("error"):
        path = result.get("path", "")
        n = result.get("occurrences_found", result.get("replaced", 0))
        check = c(C.GREEN, "✓")
        detail = f" ({n} occurrence(s))" if n else ""
        print(f"  {c(C.GRAY_DARK, '└')} {check} {c(C.GRAY, str(path))}{c(C.DIM, detail)}")
        return

    # project_overview returns a multi-key dict (project_type, project_files,
    # listing, git, path) — the generic summary picks just `path` and hides
    # everything useful. Render the key facts inline instead.
    if name == "project_overview" and "project_type" in result:
        types = ", ".join(result.get("project_type") or ["unknown"])
        files = result.get("project_files") or []
        lines = [f"type: {types}"]
        if files:
            lines.append(f"files: {', '.join(files)}")
        _print_result_body(lines)
        return

    output = (
        result.get("output")
        or result.get("content")
        or result.get("result")
        or result.get("stdout")
    )
    if isinstance(output, str) and output.strip():
        _print_result_body(output.strip().split("\n"))
    else:
        _print_result_body([_result_summary_line(result)])


# Session-level approval state
_approve_all: bool = False


def reset_approve_all() -> None:
    """Reset the approve-all state (call on /clear or new session)."""
    global _approve_all
    _approve_all = False


def is_auto_accept() -> bool:
    """Whether the session is currently auto-approving destructive tools."""
    return _approve_all


def set_auto_accept(value: bool) -> None:
    """Explicitly turn auto-accept on/off. Used by /accept-edits and shift+tab."""
    global _approve_all
    _approve_all = bool(value)


def toggle_auto_accept() -> bool:
    """Flip auto-accept and return the new state."""
    global _approve_all
    _approve_all = not _approve_all
    return _approve_all


def _print_plan_card(args: dict) -> None:
    """Pretty-print a present_plan approval card."""
    summary = str(args.get("summary", ""))
    steps = args.get("steps", []) or []
    print()
    print(f"  {c(C.YELLOW + C.BOLD, '┌─ PLANO PROPOSTO ─────────────────────')}")
    print(f"  {c(C.YELLOW, '│')} {c(C.WHITE + C.BOLD, summary)}")
    print(f"  {c(C.YELLOW, '│')}")
    for i, step in enumerate(steps, start=1):
        text = str(step)
        if len(text) > DISPLAY_PROMPT_VALUE_TRUNCATE:
            text = text[:DISPLAY_PROMPT_VALUE_TRUNCATE - 3] + "..."
        print(f"  {c(C.YELLOW, '│')} {c(C.GRAY, f'{i:>2}.')} {text}")
    print(f"  {c(C.YELLOW + C.BOLD, '└──────────────────────────────────────')}")


def print_approval_request(tool_name: str, args: dict) -> bool:
    """Show approval request with Kali-style danger indication.

    Returns True if approved. Supports:
    - s/y: approve this action
    - n: deny this action
    - a: approve ALL actions for the rest of this session
    """
    global _approve_all

    # If user previously chose "approve all", auto-approve
    if _approve_all:
        print(f"  {c(C.GREEN, '✦')} {c(C.CYAN, tool_name)} {c(C.GREEN_DARK, '(auto-approved)')}")
        return True

    if tool_name == "present_plan":
        _print_plan_card(args)
    else:
        print()
        print(f"  {c(C.RED + C.BOLD, '┌─ APROVAÇÃO NECESSÁRIA ─────────────────────')}")
        print(f"  {c(C.RED, '│')} Tool: {c(C.CYAN + C.BOLD, tool_name)}")
        if isinstance(args, dict):
            for k, v in args.items():
                val_str = str(v)
                if len(val_str) > DISPLAY_PROMPT_VALUE_TRUNCATE:
                    val_str = val_str[:DISPLAY_PROMPT_VALUE_TRUNCATE - 3] + "..."
                print(f"  {c(C.RED, '│')} {c(C.GRAY, k)}: {val_str}")
        print(f"  {c(C.RED + C.BOLD, '└────────────────────────────────────────')}")

    try:
        while True:
            resp = input(
                f"\n  {c(C.YELLOW + C.BOLD, 'Aprovar? [s/n/a(ll)]:')} "
            ).strip().lower()
            if resp in ("s", "sim", "y", "yes"):
                print(f"  {c(C.GREEN, '✓ Aprovado')}")
                return True
            if resp in ("n", "não", "nao", "no"):
                print(f"  {c(C.RED, '✗ Negado')}")
                return False
            if resp in ("a", "all", "todos"):
                _approve_all = True
                print(f"  {c(C.GREEN + C.BOLD, '✓ Aprovado (all para esta sessão)')}")
                return True
    except EOFError:
        print(f"  {c(C.GRAY, '(auto-denied — sem terminal interativo)')}")
        return False
    except KeyboardInterrupt:
        # Sem este handler, Ctrl+C durante o prompt mata o REPL inteiro.
        # Tratar como "negado" e devolver controle preserva a sessao.
        print(f"\n  {c(C.RED, '✗ Negado (Ctrl+C)')}")
        return False


def print_phase(detail: str) -> None:
    """Display a phase/progress update."""
    print(f"  {c(C.VIOLET_DARK, '→')} {c(C.DIM, detail)}")


def print_error(message: str) -> None:
    """Display an error message in red with border."""
    print(f"\n  {c(C.RED + C.BOLD, '✗ Error:')} {c(C.RED, message)}")


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


def print_subagent_event(event: dict, agent_label: str = "") -> None:
    """Display a sub-agent event indented one level under the parent.

    Uses the same `● Name(args)` / `└ result` look as the top-level tools,
    just shifted right with `  ⚤` as the agent gutter so the hierarchy
    reads at a glance.
    """
    gutter = c(C.MAGENTA, "⚤")
    label_str = c(C.MAGENTA + C.DIM, agent_label) if agent_label else ""

    event_type = event.get("type", "")
    if event_type == "tool_call":
        header = _format_tool_call_header(
            event.get("name", ""),
            event.get("args", {}),
            event.get("safety", "safe"),
        )
        if label_str:
            print(f"  {gutter} {label_str}  {header}")
        else:
            print(f"  {gutter} {header}")
    elif event_type == "done":
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


def label_for_tool(name: str) -> str:
    """Map a tool name to a short phase verb shown in the indicator."""
    if not name:
        return "Working"
    n = name.lower()
    if n.startswith("mcp__"):
        return "Calling MCP"
    if n in {"read_file", "list_directory", "list_tables"}:
        return "Reading"
    if n in {"glob_files", "search_files", "project_overview"}:
        return "Searching"
    if n in {"edit_file", "write_file", "search_and_replace"}:
        return "Editing"
    if n in {"execute_shell", "execute_pipeline", "execute_python"}:
        return "Bash"
    if n in {"http_request", "web_search", "apify_run_actor", "apify_search_actors"}:
        return "Fetching"
    if n in {"delegate_task", "delegate_parallel"}:
        return "Delegating"
    if n in {"present_plan", "todo_write"}:
        return "Planning"
    if n in {"query_database", "describe_table"}:
        return "Querying"
    if n in {"run_tests", "deploy_check"}:
        return "Testing"
    if n == "git_operation":
        return "Git"
    if n == "screenshot":
        return "Capturing"
    if n == "install_package":
        return "Installing"
    if n == "load_skill":
        return "Loading skill"
    if n == "notify_user":
        return "Notifying"
    if n in {"clipboard_read", "clipboard_write"}:
        return "Clipboard"
    return "Working"


