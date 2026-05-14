"""
Terminal display helpers for Alpha Code.

Kali Linux-inspired color scheme with priority-based visual indicators.
Green/red dominant palette, safety-aware tool display, hacker aesthetic.
"""

import asyncio
import json
import os
import re
import shutil
import sys
import time

from . import __version__

# ─── Display truncation constants (#D010 V1.0) ───
#
# Antes esses limites viviam inline em ~6 funcoes diferentes (`[:200]`,
# `[:120]`, `[:97]+"..."`, `max_lines = 8`). Centralizar em um lugar
# unico permite ajuste consistente e elimina mismatches silenciosos.
DISPLAY_LINE_TRUNCATE = 200       # max chars per terminal line
DISPLAY_PREVIEW_TRUNCATE = 120    # last-reply preview / TUI status
DISPLAY_PROMPT_VALUE_TRUNCATE = 100  # approval prompt arg values (followed by ...)
DISPLAY_MAX_LINES = 8             # max lines from a tool result
DISPLAY_SUBAGENT_TRUNCATE = 80    # sub-agent event arg preview

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences so truncation doesn't cut mid-code."""
    return _ANSI_RE.sub('', text)


# ─── ANSI Colors (Kali Linux palette) ───


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Core palette (Kali-inspired: green dominant, red for danger)
    GREEN = "\033[38;5;82m"       # Bright green (Kali signature)
    GREEN_DARK = "\033[38;5;34m"  # Dark green (secondary)
    GREEN_NEON = "\033[38;5;46m"  # Neon green (thinking indicator)
    RED = "\033[38;5;196m"        # Bright red (errors/critical, also brand)
    RED_DARK = "\033[38;5;124m"   # Dark red (warnings, brand borders)
    # Soft coral red used as the pulse-peak shade on the thinking indicator —
    # close enough to RED that the alternation reads as "alive" rather than
    # flicker, but desaturated enough not to look like an alert.
    RED_GLOW = "\033[38;5;203m"   # #ff5f5f (coral red, pulse peak)
    YELLOW = "\033[38;5;220m"     # Yellow (caution/approval)
    ORANGE = "\033[38;5;208m"     # Orange (medium priority)
    BLUE = "\033[38;5;33m"        # Blue (info)
    CYAN = "\033[38;5;51m"        # Cyan (tool names)
    MAGENTA = "\033[38;5;135m"    # Purple (sub-agents)
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
    "safe": "✦",
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


def print_tool_call(name: str, args: dict, safety: str = "safe") -> None:
    """Display a tool call with safety-colored indicator."""
    args_str = ""
    # Tools with dedicated renderers (checklist, plan card) — skip the
    # generic args preview, otherwise we dump raw list-of-dicts above
    # the pretty output.
    if name in {"todo_write", "present_plan"}:
        pass
    elif isinstance(args, dict):
        for key in ("path", "command", "query", "action", "pattern", "file", "code"):
            if key in args:
                val = str(args[key])
                if len(val) > DISPLAY_PREVIEW_TRUNCATE:
                    val = val[:DISPLAY_PREVIEW_TRUNCATE - 3] + "..."
                args_str = f" {c(C.GRAY, val)}"
                break
        if not args_str and args:
            first_val = str(next(iter(args.values())))
            if len(first_val) > DISPLAY_PREVIEW_TRUNCATE:
                first_val = first_val[:DISPLAY_PREVIEW_TRUNCATE - 3] + "..."
            args_str = f" {c(C.GRAY, first_val)}"

    safety_color = _SAFETY_COLORS.get(safety, C.YELLOW)
    icon = c(safety_color, _SAFETY_ICONS.get(safety, "⚡"))
    label = _display_tool_name(name)
    tool_name = c(C.CYAN + C.BOLD, label) if safety == "safe" else c(safety_color + C.BOLD, label)

    print(f"  {icon} {tool_name}{args_str}")


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
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.BG_GREEN + C.GREEN, '+ ' + text)}")
        elif line.startswith("-"):
            text = line[1:]
            if len(text) > DISPLAY_LINE_TRUNCATE:
                text = text[:DISPLAY_LINE_TRUNCATE - 3] + "..."
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.BG_RED + C.RED, '- ' + text)}")
        else:
            text = line[1:] if line.startswith(" ") else line
            if len(text) > DISPLAY_LINE_TRUNCATE:
                text = text[:DISPLAY_LINE_TRUNCATE - 3] + "..."
            print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, '  ' + text)}")
        shown += 1

    print(f"  {c(C.GRAY_DARK, '└─')}")


def print_tool_result(name: str, result: dict, args: dict | None = None) -> None:
    """Display a tool result with status-aware formatting.

    For `edit_file` / `write_file`, renders a colored unified diff from the
    args passed in (old_text/new_text or new content vs current file).
    """
    border = c(C.GRAY_DARK, "│")

    if isinstance(result, dict):
        # Error results in red
        if result.get("error"):
            print(f"  {c(C.RED, '✗')} {c(C.RED, _strip_ansi(str(result['error']))[:DISPLAY_LINE_TRUNCATE])}")
            return

        # Skipped/denied results
        if result.get("skipped"):
            reason = result.get("reason", "denied")
            print(f"  {c(C.YELLOW, '⊘')} {c(C.YELLOW, reason[:DISPLAY_LINE_TRUNCATE])}")
            return

        # todo_write — pin checklist above the spinner when scroll region is
        # active, otherwise fall back to inline print so the user always sees
        # the list (non-TTY, or terminal without scroll-region support).
        if name == "todo_write" and isinstance(result.get("todos"), list):
            todos = result["todos"]
            ind = _active_indicator
            if ind is not None and ind._scroll_active:
                set_pinned_todos(todos)
            else:
                _print_todo_list(todos)
            warning = result.get("warning")
            if warning:
                print(f"  {c(C.YELLOW, '⚠')} {c(C.YELLOW, warning)}")
            return

        # Diff rendering for edits — show what was added vs. removed.
        if name == "edit_file" and isinstance(args, dict) and args.get("old_text"):
            old_text = str(args.get("old_text", ""))
            new_text = str(args.get("new_text", ""))
            path = result.get("path") or args.get("path")
            _render_diff(old_text, new_text, str(path) if path else None)
            return

        if name == "write_file" and isinstance(args, dict) and args.get("content"):
            new_content = str(args.get("content", ""))
            path = result.get("path") or args.get("path")
            old_content = result.get("_previous_content", "")
            _render_diff(old_content, new_content, str(path) if path else None)
            return

        # Clean one-line summary for file ops without diff args (parallel batches).
        if name in ("edit_file", "write_file") and not result.get("error"):
            path = result.get("path", "")
            n = result.get("occurrences_found", result.get("replaced", 0))
            ok = c(C.GREEN, "✓") if not result.get("skipped") else c(C.YELLOW, "⊘")
            detail = f"{n} occurrence(s)" if n else ""
            print(f"  {ok} {c(C.CYAN, name)} {c(C.GRAY, str(path))} {c(C.DIM, detail)}".rstrip())
            return

        # Show output/content preview
        output = result.get("output") or result.get("content") or result.get("result")
        if isinstance(output, str) and output.strip():
            lines = output.strip().split("\n")
            for line in lines[:DISPLAY_MAX_LINES]:
                print(f"  {border} {_strip_ansi(line)[:DISPLAY_LINE_TRUNCATE]}")
            if len(lines) > DISPLAY_MAX_LINES:
                remaining = len(lines) - DISPLAY_MAX_LINES
                print(f"  {border} {c(C.GRAY, f'... ({remaining} more lines)')}")
        else:
            # Compact one-line summary for results without text output.
            # Pick the most informative key: path > count > status > first value.
            short = result.get("path") or str(result.get("count", ""))
            if not short:
                keys = [k for k in result if not k.startswith("_")]
                if keys:
                    first_val = str(result[keys[0]])
                    short = first_val[:DISPLAY_LINE_TRUNCATE - 20]
            if len(str(short)) > DISPLAY_LINE_TRUNCATE - 10:
                short = str(short)[:DISPLAY_LINE_TRUNCATE - 13] + "..."
            print(f"  {border} {c(C.GRAY, str(short))}")
    else:
        result_str = _strip_ansi(str(result))[:DISPLAY_LINE_TRUNCATE]
        print(f"  {border} {result_str}")


# Session-level approval state
_approve_all: bool = False


def reset_approve_all() -> None:
    """Reset the approve-all state (call on /clear or new session)."""
    global _approve_all
    _approve_all = False


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
        print(f"  {c(C.RED + C.BOLD, '┌─ APPROVAL NEEDED ─────────────────────')}")
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
    print(f"  {c(C.RED_DARK, '→')} {c(C.DIM, detail)}")


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


def print_subagent_event(event: dict, agent_label: str = "") -> None:
    """Display a sub-agent event with indentation."""
    prefix = f"  {c(C.GRAY_DARK, '┊')}"
    label = f" {c(C.MAGENTA + C.BOLD, agent_label)}" if agent_label else ""

    event_type = event.get("type", "")
    if event_type == "tool_call":
        name = event.get("name", "")
        args = event.get("args", {})
        safety = event.get("safety", "safe")
        safety_color = _SAFETY_COLORS.get(safety, C.YELLOW)
        icon = c(safety_color, _SAFETY_ICONS.get(safety, "✦"))
        args_str = ""
        for key in ("path", "command", "query", "action", "pattern"):
            if key in args:
                val = str(args[key])
                if len(val) > DISPLAY_SUBAGENT_TRUNCATE:
                    val = val[:DISPLAY_SUBAGENT_TRUNCATE - 3] + "..."
                args_str = f" {c(C.GRAY, val)}"
                break
        print(f"{prefix}{label} {icon} {c(C.CYAN, _display_tool_name(name))}{args_str}")
    elif event_type == "done":
        reply = event.get("reply", "")
        preview = reply[:DISPLAY_PREVIEW_TRUNCATE].replace("\n", " ") if reply else ""
        print(f"{prefix}{label} {c(C.GREEN, '✓')} {c(C.DIM, preview)}")


def print_tools_list(tools: list[dict]) -> None:
    """Display tools grouped by category with safety indicators."""
    if not tools:
        print(c(C.GRAY, "  No tools loaded."))
        return

    # Group by category
    categories: dict[str, list[dict]] = {}
    for t in tools:
        fn = t.get("function", {})
        # Try to get category from description or name patterns
        name = fn.get("name", "")
        desc = fn.get("description", "")

        # Infer category from tool name prefix
        cat = "general"
        for prefix in ("read_file", "write_file", "edit_file", "list_directory",
                        "search_files", "glob_files", "search_and_replace"):
            if name == prefix:
                cat = "filesystem"
                break
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
        elif name in ("project_overview", "run_tests", "deploy_check"):
            cat = "composite"
        elif name.startswith("search"):
            cat = "search"
        elif name.startswith("system_") or name.startswith("env_"):
            cat = "system"
        elif name.startswith("browser_"):
            cat = "browser"
        elif name.startswith("apify_"):
            cat = "scraping"

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
    """Display the Mythos startup banner — skull + hacker terminal aesthetic."""
    cwd = os.getcwd()

    skull = r"""
           .--------.
          /   ,----.  \
         /   /      \  \
        /   |  O  O  |  \
       /    |    ∆   |   \
      /     \  `---´ /    \
     /       `.____.´      \
    /     .-------------.   \
   /    .´               `.  \
  /   .´  ██╗  ██╗██╗   ██╗ `. \
 /   /    ███╗██╔╝██║   ██║  \  \
/   /     ╚███╔╝ ███████║  \   \
\   \    ██╗██╗ ██╔══██║   /   /
 \   \    ╚═╝╚═╝ ╚═╝  ╚═╝  /   /
  \   `.                 .´   /
   \    `-.           .-´    /
    \       `-------´      /
     \   ▄▄  HUNTER  ▄▄  /
      \                   /
       `-----------------´"""

    mythos_logo = r"""
  ╔══════════════════════════════════════════════════════╗
  ║                                                      ║
  ║   ███╗   ███╗██╗   ██╗████████╗██╗  ██╗ ██████╗     ║
  ║   ████╗ ████║╚██╗ ██╔╝╚══██╔══╝██║  ██║██╔═══██╗    ║
  ║   ██╔████╔██║ ╚████╔╝    ██║   ███████║██║   ██║    ║
  ║   ██║╚██╔╝██║  ╚██╔╝     ██║   ██╔══██║██║   ██║    ║
  ║   ██║ ╚═╝ ██║   ██║      ██║   ██║  ██║╚██████╔╝    ║
  ║   ╚═╝     ╚═╝   ╚═╝      ╚═╝   ╚═╝  ╚═╝ ╚═════╝     ║
  ║                                                      ║
  ║   ██╗   ██╗██╗   ██╗██╗     ███╗   ██╗                ║
  ║   ██║   ██║██║   ██║██║     ████╗  ██║                ║
  ║   ██║   ██║██║   ██║██║     ██╔██╗ ██║                ║
  ║   ██║   ██║██║   ██║██║     ██║╚██╗██║                ║
  ║   ╚██████╔╝╚██████╔╝███████╗██║ ╚████║                ║
  ║    ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═══╝                ║
  ║                                                      ║
  ║     MAP ▸ NARROW ▸ INSPECT ▸ EXPLOIT  ·  v2.0         ║
  ╚══════════════════════════════════════════════════════╝"""

    print(c(C.RED + C.BOLD, mythos_logo))
    print(f"  {c(C.RED, '▸')} {c(C.RED_DARK, 'ver   ')} {c(C.RED_GLOW, f'v{__version__}')}")
    print(f"  {c(C.RED, '▸')} {c(C.RED_DARK, 'path  ')} {c(C.GRAY, cwd)}")
    print(f"  {c(C.RED, '▸')} {c(C.RED_DARK, 'model ')} {c(C.CYAN, provider)} {c(C.GRAY, f'({model})')}")
    print(f"  {c(C.RED, '▸')} {c(C.RED_DARK, 'cmds  ')} {c(C.GRAY, '/clear /history /continue /tools /model /help /exit')}")
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


# ─── Thinking indicator (spinner) ───

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_FLOWER_FRAMES = ["✻", "✽", "✾", "✿", "❀", "✿", "✾", "✽"]

# Switches every ~8s so the user sees motion when the LLM is silent.
# Cybersec-leaning verb set — keeps the recon vibe of `label_for_tool`
# (Attacking/Scanning) without sliding into theatrics during idle thinks.
_THINK_VERBS = (
    "Thinking",
    "Probing",
    "Reconnoitring",
    "Inspecting",
    "Profiling",
    "Analyzing",
    "Investigating",
    "Hunting",
    "Mapping",
    "Tracing",
    "Considering",
    "Evaluating",
)
_VERB_ROTATE_SECS = 8

_HINT_PHRASES = (
    (8, "warming up"),
    (20, "exploring"),
    (45, "deep in thought"),
    (90, "iterating"),
    (180, "almost done thinking"),
    (360, "still going"),
)


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


def _hint_for(seconds: float) -> str:
    last = ""
    for threshold, phrase in _HINT_PHRASES:
        if seconds >= threshold:
            last = phrase
        else:
            break
    return last


# Tool → phase verb mapping. Dict lookup substitui a if-chain anterior
# (#DM032 — colapso de 30 linhas em 1 dict; O(1) em vez de O(n) por tool).
_TOOL_LABEL_MAP: dict[frozenset[str], str] = {
    frozenset({"read_file", "view_file", "read"}): "Reading",
    frozenset({"glob_files", "search_files", "grep_files", "find_files", "grep"}): "Searching",
    frozenset({"edit_file", "write_file", "multi_edit", "search_and_replace"}): "Editing",
    frozenset({"run_shell", "run_command", "shell", "bash", "execute_shell",
               "execute_python", "execute_pipeline"}): "Bash",
    frozenset({"delegate_task", "delegate_parallel"}): "Delegating",
    frozenset({"present_plan", "todo_write"}): "Planning",
    frozenset({"web_fetch", "web_search", "http_request", "fetch"}): "Fetching",
    frozenset({"run_tests", "deploy_check"}): "Testing",
    frozenset({"query_database", "describe_table"}): "Querying",
    frozenset({"scan_vulnerabilities", "audit_dependencies", "scan_binary",
               "fuzz_endpoint", "check_misconfigurations"}): "Scanning",
    frozenset({"nmap_scan", "ffuf_fuzz", "banner_grab", "payload_inject",
               "traffic_capture", "port_knock", "exploit_loop"}): "Attacking",
}


def label_for_tool(name: str) -> str:
    """Map a tool name to a short phase verb shown in the indicator."""
    if not name:
        return "Working"
    n = name.lower()
    if n.startswith("mcp__"):
        return "Calling MCP"
    for keys, label in _TOOL_LABEL_MAP.items():
        if n in keys:
            return label
    return "Working"


class ThinkingIndicator:
    """Animated spinner that always stays on the bottom row of the terminal.

    Uses ANSI scroll regions (DECSTBM) to reserve the last terminal row(s)
    for the spinner + status + pinned-todo panel, so streaming tokens, tool
    calls and sub-agent events all flow above without erasing the spinner.

    Falls back to inline mode (\\r-rewrite on the current line) when stdout
    is not a TTY or scroll-region setup is suppressed.
    """

    _DISABLE_SCROLL_ENV = "ALPHA_NO_SCROLL_REGION"
    # spinner + status + bottom padding (keeps the indicator from being
    # glued to the terminal's bottom edge — same breathing room Claude
    # Code shows below its toolbar).
    _BASE_RESERVED = 3
    _MAX_PANEL_ROWS = 12

    def __init__(self, label: str = "Think", style: str = "flower") -> None:
        self.label = label
        self.frames = _FLOWER_FRAMES if style == "flower" else _SPINNER_FRAMES
        self._task: asyncio.Task | None = None
        self._running = False
        self._paused = False
        self._start_time = 0.0
        self._enabled = supports_color()
        self._scroll_active = False
        self._term_rows = 0
        self._term_cols = 0
        self._streamed_chars = 0
        self._panel_capacity = 0

    # ── Scroll region lifecycle ─────────────────────────────────

    def _detect_size(self) -> tuple[int, int]:
        try:
            sz = shutil.get_terminal_size((80, 24))
            return sz.lines, sz.columns
        except Exception:
            return 24, 80

    def _scroll_supported(self) -> bool:
        if not self._enabled:
            return False
        if os.environ.get(self._DISABLE_SCROLL_ENV):
            return False
        term = os.environ.get("TERM", "").lower()
        if term in {"dumb", ""}:
            return False
        return True

    def _desired_panel_rows(self, term_rows: int) -> int:
        todos = _pinned_todos or []
        if not todos:
            return 0
        wanted = min(len(todos), self._MAX_PANEL_ROWS) + (
            1 if len(todos) > self._MAX_PANEL_ROWS else 0
        )
        ceiling = max(0, term_rows - self._BASE_RESERVED - 4)
        return min(wanted, ceiling)

    def _total_reserved(self) -> int:
        return self._panel_capacity + self._BASE_RESERVED

    def _setup_scroll(self) -> bool:
        if self._scroll_active:
            return True
        if not self._scroll_supported():
            return False
        rows, cols = self._detect_size()
        if rows < self._BASE_RESERVED + 2:
            return False
        self._term_rows = rows
        self._term_cols = cols
        self._panel_capacity = self._desired_panel_rows(rows)
        reserved = self._total_reserved()
        scroll_bottom = rows - reserved
        out = (
            "\n" * reserved
            + f"\033[1;{scroll_bottom}r"
            + f"\033[{scroll_bottom};1H"
        )
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            return False
        self._scroll_active = True
        return True

    def _teardown_scroll(self) -> None:
        if not self._scroll_active:
            return
        rows = self._term_rows or self._detect_size()[0]
        reserved = self._total_reserved()
        clears = "".join(
            f"\033[{rows - i};1H\033[K"
            for i in range(reserved - 1, -1, -1)
        )
        out = clears + "\033[r" + f"\033[{rows};1H"
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            pass
        self._scroll_active = False
        self._panel_capacity = 0

    def _maybe_resize(self) -> None:
        if not self._scroll_active:
            return
        rows, cols = self._detect_size()
        target_capacity = self._desired_panel_rows(rows)
        if (
            rows == self._term_rows
            and cols == self._term_cols
            and target_capacity == self._panel_capacity
        ):
            return
        self._teardown_scroll()
        self._setup_scroll()

    def refresh_layout(self) -> None:
        if not self._scroll_active or not self._enabled:
            self._draw()
            return
        target = self._desired_panel_rows(self._term_rows or self._detect_size()[0])
        if target != self._panel_capacity:
            self._teardown_scroll()
            self._setup_scroll()
        self._draw()

    # ── Frame rendering ─────────────────────────────────────────

    def _select_verb(self, elapsed: float) -> str:
        """Rotate `_THINK_VERBS` when label is "Think"; otherwise verbatim."""
        if self.label == "Think":
            idx = int(elapsed / _VERB_ROTATE_SECS) % len(_THINK_VERBS)
            return _THINK_VERBS[idx]
        return self.label

    def _build_frame(self) -> str:
        elapsed = time.monotonic() - self._start_time
        anim_idx = int(elapsed / 0.12)
        frame = self.frames[anim_idx % len(self.frames)]
        verb = self._select_verb(elapsed)

        parts: list[str] = []
        dur = _format_duration(elapsed)
        if dur:
            parts.append(dur)
        token_estimate = self._streamed_chars // 4
        if token_estimate > 0:
            parts.append(f"↓ {_format_tokens(token_estimate)} tokens")
        hint = _hint_for(elapsed)
        if hint:
            parts.append(hint)

        if parts:
            paren = c(C.GRAY, " (" + " · ".join(parts) + ")")
        else:
            paren = ""

        spinner_part = c(C.ORANGE + C.BOLD, frame)
        # Breathe between RED and RED_GLOW over a 2-second cycle — alive
        # but not flicker-y. `int(elapsed * 2) % 4` ticks once per 0.5s.
        pulse = C.RED_GLOW if int(elapsed * 2) % 4 >= 2 else C.RED
        verb_part = c(pulse + C.BOLD, f"{verb}…")
        return f"{spinner_part} {verb_part}{paren}"

    _STATUS_VARIANTS = (
        ("(shift+tab to cycle)", len("▸▸ accept edits on (shift+tab to cycle)") + 2),
        ("(shift+tab)",          len("▸▸ accept edits on (shift+tab)") + 2),
        ("",                     len("▸▸ accept edits on") + 2),
    )

    def _build_status(self) -> str:
        """Bottom reserved row — accept-edits state mirror. Empty when off."""
        if not _approve_all:
            return ""
        cols = self._term_cols or self._detect_size()[1]
        prefix = (
            f"{c(C.YELLOW + C.BOLD, '▸▸')} "
            f"{c(C.YELLOW + C.BOLD, 'accept edits on')}"
        )
        for suffix, min_cols in self._STATUS_VARIANTS:
            if cols >= min_cols:
                if not suffix:
                    return prefix
                return f"{prefix} {c(C.GRAY_DARK, suffix)}"
        return ""

    def _build_panel_lines(self) -> list[str]:
        todos = _pinned_todos or []
        if not todos or self._panel_capacity == 0:
            return []
        cols = self._term_cols or self._detect_size()[1]
        max_content = max(20, cols - 6)
        overflow = len(todos) > self._panel_capacity
        visible_count = self._panel_capacity - 1 if overflow else self._panel_capacity
        lines: list[str] = []
        for t in todos[:visible_count]:
            if not isinstance(t, dict):
                continue
            status = str(t.get("status", "pending"))
            glyph, color = _TODO_STATUS_GLYPH.get(status, ("•", C.GRAY))
            content = str(t.get("content", ""))
            if len(content) > max_content:
                content = content[: max_content - 1] + "…"
            line_color = C.GRAY if status in ("completed", "cancelled") else C.WHITE
            lines.append(f"  {c(color, glyph)} {c(line_color, content)}")
        if overflow:
            remaining = len(todos) - visible_count
            lines.append(f"  {c(C.GRAY_DARK, f'… +{remaining} more')}")
        return lines

    def _draw(self) -> None:
        if not self._running or self._paused or not self._enabled:
            return
        frame_text = self._build_frame()

        if not self._scroll_active:
            sys.stdout.write(f"\r{frame_text}\033[K")
            sys.stdout.flush()
            return

        status_text = self._build_status()
        panel_lines = self._build_panel_lines()
        rows = self._term_rows
        reserved = self._total_reserved()
        # Layout (top→bottom): panel rows · spinner · status · blank pad.
        # Bottom row stays blank so the indicator isn't glued to the edge.
        panel_top = rows - reserved + 1
        spinner_row = rows - 2
        status_row = rows - 1

        out_parts = ["\033[s"]
        for i, line in enumerate(panel_lines):
            out_parts.append(f"\033[{panel_top + i};1H\033[K{line}")
        out_parts.append(f"\033[{spinner_row};1H\033[K{frame_text}")
        out_parts.append(f"\033[{status_row};1H\033[K{status_text}")
        out_parts.append("\033[u")
        try:
            sys.stdout.write("".join(out_parts))
            sys.stdout.flush()
        except Exception:
            pass

    # ── Public API ──────────────────────────────────────────────

    def start(self, label: str | None = None) -> None:
        global _active_indicator
        if not self._enabled:
            if label:
                self.label = label
            return
        if self._running:
            if label:
                self.label = label
            self._paused = False
            self._draw()
            return
        if label:
            self.label = label
        self._setup_scroll()
        self._running = True
        self._paused = False
        self._start_time = time.monotonic()
        _active_indicator = self
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._animate())
        except RuntimeError:
            self._running = False
            self._teardown_scroll()
            if _active_indicator is self:
                _active_indicator = None

    def stop(self) -> None:
        global _active_indicator
        if not self._running:
            if self._scroll_active:
                self._teardown_scroll()
            if _active_indicator is self:
                _active_indicator = None
            return
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._scroll_active:
            self._teardown_scroll()
        elif self._enabled:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        if _active_indicator is self:
            _active_indicator = None

    def pause(self) -> None:
        """Suppress redraws without killing the task. Used during input().
        Clears the spinner + status rows but leaves the todo panel visible."""
        self._paused = True
        if self._scroll_active and self._enabled:
            rows = self._term_rows
            clears = "".join(
                f"\033[{rows - i};1H\033[K"
                for i in range(self._BASE_RESERVED - 1, -1, -1)
            )
            try:
                sys.stdout.write(f"\033[s{clears}\033[u")
                sys.stdout.flush()
            except Exception:
                pass

    def resume(self) -> None:
        self._paused = False
        self._draw()

    def update_label(self, label: str) -> None:
        self.label = label
        self._draw()

    def add_streamed_text(self, text: str) -> None:
        """Track chars streamed during the current turn — surfaced in the
        spinner's parens as `↓ <count> tokens` (estimated at render time)."""
        if text:
            self._streamed_chars += len(text)

    async def _animate(self) -> None:
        try:
            while self._running:
                self._maybe_resize()
                self._draw()
                await asyncio.sleep(0.12)
        except asyncio.CancelledError:
            pass


_active_indicator: "ThinkingIndicator | None" = None

# Most recent todo list pinned above the spinner. Survives across turns
# until cleared so the user keeps the checklist context between prompts.
_pinned_todos: "list[dict] | None" = None


def get_active_indicator() -> "ThinkingIndicator | None":
    return _active_indicator


def set_pinned_todos(todos: "list[dict] | None") -> None:
    """Pin a todo list above the spinner. Empty/None clears."""
    global _pinned_todos
    _pinned_todos = list(todos) if todos else None
    ind = _active_indicator
    if ind is not None:
        ind.refresh_layout()


def get_pinned_todos() -> "list[dict] | None":
    return _pinned_todos


def cleanup_indicator() -> None:
    """atexit hook — reset scroll region if the agent crashes before stop()."""
    ind = _active_indicator
    if ind is None:
        return
    try:
        ind.stop()
    except Exception:
        pass
