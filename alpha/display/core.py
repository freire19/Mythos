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
import textwrap
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

    # Core palette — solid tones. GREEN_NEON is reserved for the
    # thinking-indicator pulse; everything else stays muted enough that
    # the violet brand reads as the loudest thing on the screen.
    GREEN = "\033[38;5;34m"       # #00af00 forest green
    GREEN_DARK = "\033[38;5;28m"  # #008700
    GREEN_NEON = "\033[38;5;46m"  # #00ff00 (thinking pulse only)
    RED = "\033[38;5;160m"        # #d70000
    RED_DARK = "\033[38;5;124m"   # #af0000
    YELLOW = "\033[38;5;178m"     # #d7af00 gold
    ORANGE = "\033[38;5;172m"     # #d78700
    BLUE = "\033[38;5;33m"        # #0087ff
    CYAN = "\033[38;5;37m"        # #00afaf teal
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


def _truncate(s: str, limit: int) -> str:
    """Trim `s` to fit within `limit` characters, marking truncation with `…`."""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


# ─── Markdown rendering for LLM responses ───
#
# Order matters: code spans first (so we never style `**bold**` inside `code`),
# then bold (must catch `**` before italic's `*`), then italic.
_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


_TABLE_MIN_COL_WIDTH = 6
_TABLE_CELL_PADDING = 3  # one bar + space-pad-space around the content


def _table_column_widths(cells: list[list[str]], cols: int) -> list[int]:
    # Shrink widest column iteratively until the table fits the terminal width
    # or every column hits _TABLE_MIN_COL_WIDTH.
    natural = [
        max((len(r[i]) for r in cells if i < len(r)), default=1)
        for i in range(cols)
    ]
    term_w = shutil.get_terminal_size((80, 24)).columns
    chrome = _TABLE_CELL_PADDING * cols + 1  # bars + 2-space pad per cell + closing bar
    budget = max(_TABLE_MIN_COL_WIDTH * cols, term_w - chrome)

    widths = list(natural)
    while sum(widths) > budget:
        widest = max(widths)
        if widest <= _TABLE_MIN_COL_WIDTH:
            break  # all at floor — let it overflow rather than mangle further
        idx = widths.index(widest)
        widths[idx] -= 1
    return widths


def _wrap_cell(text: str, width: int) -> list[str]:
    # Wrap cell text to `width`; keeps file paths like `alpha/x.py:38` together
    # (break_on_hyphens=False) but chops tokens that still overflow.
    if not text:
        return [""]
    wrapped = textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        drop_whitespace=False,
    )
    return wrapped or [""]


def _render_md_table(lines: list[str]) -> list[str]:
    # Format a markdown table as ASCII with violet separators, adapting
    # column widths to the terminal via cell wrapping.
    cells: list[list[str]] = []
    for ln in lines:
        if _MD_TABLE_SEP_RE.match(ln):
            continue
        row = [p.strip() for p in ln.strip().strip("|").split("|")]
        cells.append(row)
    if not cells:
        return lines
    cols = max(len(r) for r in cells)
    cells = [r + [""] * (cols - len(r)) for r in cells]

    widths = _table_column_widths(cells, cols)
    bar = c(C.VIOLET_DARK, "│")
    out: list[str] = []
    for row_idx, r in enumerate(cells):
        wrapped_cells = [_wrap_cell(cell, widths[i]) for i, cell in enumerate(r)]
        n_lines = max(len(w) for w in wrapped_cells)
        for ln_idx in range(n_lines):
            parts: list[str] = []
            for col_idx in range(cols):
                lines_for_cell = wrapped_cells[col_idx]
                text = lines_for_cell[ln_idx] if ln_idx < len(lines_for_cell) else ""
                padded = f" {text.ljust(widths[col_idx])} "
                if row_idx == 0:
                    padded = c(C.WHITE + C.BOLD, padded)
                parts.append(padded)
            out.append(bar + bar.join(parts) + bar)
        if row_idx == 0:
            sep_parts = [c(C.VIOLET_DARK, "─" * (widths[i] + 2)) for i in range(cols)]
            out.append(bar + bar.join(sep_parts) + bar)
    return out


def render_markdown(text: str) -> str:
    # Apply ANSI styling (code, bold, italic, headers, tables) to a finished
    # Markdown block. Batched end-of-turn — streaming inline rendering would
    # need a state machine across chunk boundaries.
    if NO_COLOR or not text:
        return text

    # Tables first: scan line-by-line, swap table runs in place. Detection =
    # a line containing `|` immediately followed by a `|---|---|` separator.
    raw_lines = text.split("\n")
    out_lines: list[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if (
            "|" in line
            and i + 1 < len(raw_lines)
            and _MD_TABLE_SEP_RE.match(raw_lines[i + 1])
        ):
            block_start = i
            j = i + 2
            while j < len(raw_lines) and "|" in raw_lines[j] and raw_lines[j].strip():
                j += 1
            out_lines.extend(_render_md_table(raw_lines[block_start:j]))
            i = j
            continue
        out_lines.append(line)
        i += 1
    rendered = "\n".join(out_lines)

    # Inline markup — apply in priority order.
    rendered = _MD_CODE_RE.sub(
        lambda m: f"{C.BG_GRAY}{C.WHITE} {m.group(1)} {C.RESET}", rendered
    )
    rendered = _MD_BOLD_RE.sub(lambda m: f"{C.BOLD}{m.group(1)}{C.RESET}", rendered)
    rendered = _MD_ITALIC_RE.sub(lambda m: f"{C.ITALIC}{m.group(1)}{C.RESET}", rendered)
    rendered = _MD_HEADER_RE.sub(
        lambda m: f"{C.VIOLET}{C.BOLD}{m.group(2)}{C.RESET}", rendered
    )
    return rendered

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


_HEADER_PREVIEW_KEYS = ("path", "command", "query", "action", "pattern", "file", "code")
_LIVE_LABEL_KEYS = ("path", "command", "query", "action", "pattern", "file", "task", "tasks")


def _tool_args_preview(
    args: dict,
    keys: tuple = _HEADER_PREVIEW_KEYS,
    limit: int = None,
) -> str:
    """Pick the most informative arg and truncate it. `keys` controls which
    keys are tried (default = header set); `limit` overrides truncation
    width (default = DISPLAY_PREVIEW_TRUNCATE)."""
    if not isinstance(args, dict) or not args:
        return ""
    for key in keys:
        if key in args:
            val = str(args[key])
            break
    else:
        val = str(next(iter(args.values())))
    return _truncate(val, limit or DISPLAY_PREVIEW_TRUNCATE).replace("\n", " ")


def _read_file_preview(args: dict) -> str:
    """`read_file` often gets called multiple times against the same path
    with different offset/limit ranges. Showing only `path` makes the
    consecutive calls look identical and triggers the (×N) dedup falsely.
    Append the line range when present: `path:offset-(offset+limit)`."""
    path = str(args.get("path", ""))
    offset = args.get("offset")
    limit = args.get("limit")
    pages = args.get("pages")
    if offset is not None or limit is not None:
        try:
            start = int(offset) if offset is not None else 1
            if limit is not None:
                end = start + int(limit) - 1
                suffix = f":{start}-{end}"
            else:
                suffix = f":{start}-"
        except (TypeError, ValueError):
            suffix = ""
    elif pages:
        suffix = f" pages={pages}"
    else:
        suffix = ""
    return _truncate(path + suffix, DISPLAY_PREVIEW_TRUNCATE)


def _format_tool_call_header(name: str, args: dict, safety: str) -> str:
    """Build `{icon} {ToolName}({preview})` — shared by top-level and sub-agent
    rendering so the two visual variants never drift."""
    safety_color = _SAFETY_COLORS.get(safety, C.YELLOW)
    icon = c(safety_color, _SAFETY_ICONS.get(safety, "⚡"))
    name_color = C.VIOLET if safety == "safe" else safety_color
    tool_name = c(name_color + C.BOLD, _display_tool_name(name))
    if name == "read_file" and isinstance(args, dict):
        preview = _read_file_preview(args)
    else:
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
_DIFF_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _diff_line_full_width(prefix_plain: str, bg: str, fg: str, body: str) -> str:
    """Render one diff line so the background color extends to the right
    edge of the terminal (Claude-Code style). `\\033[K` paints the rest of
    the line with whatever bg is active when it runs, so we have to keep
    the bg active and reset *after* it."""
    if NO_COLOR:
        return f"{prefix_plain}{body}"
    return f"{prefix_plain}{bg}{fg}{body}\033[K{C.RESET}"


def _render_diff(old_text: str, new_text: str, path: str | None = None) -> None:
    """Render a unified diff with green/red full-width blocks (git-style).

    Adds line numbers parsed from each `@@` hunk header and stretches the
    background color to the right edge via `\\033[K` so highlighted rows
    don't end raggedly mid-line. Output is bounded by `_DIFF_MAX_LINES`."""
    import difflib

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    if path:
        print(f"  {c(C.GRAY_DARK, '┌─')} {c(C.CYAN, path)}")

    diff = list(difflib.unified_diff(old_lines, new_lines, n=2, lineterm=""))
    if not diff:
        print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, '(no textual changes)')}")
        return

    body = [ln for ln in diff if not (ln.startswith("---") or ln.startswith("+++"))]
    added = sum(1 for ln in body if ln.startswith("+"))
    removed = sum(1 for ln in body if ln.startswith("-"))
    if added or removed:
        summary = f"Added {added} line(s), removed {removed} line(s)"
        print(f"  {c(C.GRAY_DARK, '│')} {c(C.GRAY, summary)}")

    old_n = new_n = 0
    shown = 0
    gutter = c(C.GRAY_DARK, "│")

    for line in body:
        if shown >= _DIFF_MAX_LINES:
            remaining = len(body) - shown
            print(f"  {gutter} {c(C.GRAY, f'… +{remaining} more diff lines')}")
            break

        if line.startswith("@@"):
            m = _DIFF_HUNK_RE.match(line)
            if m:
                old_n = int(m.group(1))
                new_n = int(m.group(2))
            print(f"  {gutter} {c(C.CYAN + C.DIM, line[:DISPLAY_LINE_TRUNCATE])}")
        elif line.startswith("+"):
            text = _truncate(line[1:], DISPLAY_LINE_TRUNCATE)
            num = c(C.GRAY_DARK, f"{new_n:>4}")
            print(_diff_line_full_width(
                f"  {gutter} {num} ", C.BG_GREEN, C.WHITE, f"+ {text}",
            ))
            new_n += 1
        elif line.startswith("-"):
            text = _truncate(line[1:], DISPLAY_LINE_TRUNCATE)
            num = c(C.GRAY_DARK, f"{old_n:>4}")
            print(_diff_line_full_width(
                f"  {gutter} {num} ", C.BG_RED, C.WHITE, f"- {text}",
            ))
            old_n += 1
        else:
            text = _truncate(line[1:] if line.startswith(" ") else line, DISPLAY_LINE_TRUNCATE)
            num = c(C.GRAY_DARK, f"{new_n:>4}")
            print(f"  {gutter} {num} {c(C.GRAY, '  ' + text)}")
            old_n += 1
            new_n += 1
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
    return _truncate(str(short), DISPLAY_LINE_TRUNCATE - 10)


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
    text = _truncate(text, DISPLAY_PREVIEW_TRUNCATE)
    check = c(C.GREEN, "✔")
    print(f"        {check} {c(C.GRAY, _strike(text))}")


def _print_delegate_single(task_label: str, result: dict) -> None:
    """Render a single sub-agent result as an inline Task block."""
    steps = result.get("tools_used") or []
    iterations = result.get("iterations", len(steps))
    short = _truncate(task_label, DISPLAY_PROMPT_VALUE_TRUNCATE)
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
            # Print the inline summary BEFORE pinning the panel: if the
            # panel size changes, `set_pinned_todos` triggers
            # teardown_scroll+setup_scroll, which leaves the cursor in the
            # reserved area at the bottom — a subsequent print() then
            # lands inside the panel rows and gets overwritten by the next
            # spinner redraw. Printing first keeps the line in normal
            # scroll-region territory.
            done = sum(1 for t in todos if t.get("status") == "completed")
            total = len(todos)
            print(f"  {c(C.GRAY_DARK, '└')} {c(C.GRAY, f'{total} todos pinned ({done} completed)')}")
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


# Session-level approval state.
#
# Two write paths exist:
#  - `set_auto_accept` / `toggle_auto_accept`: explicit user intent
#    (shift+tab, /accept-edits) — persisted to `~/.alpha/settings.json`
#    so the choice survives REPL restarts.
#  - In-prompt `[a]` (`print_approval_request`): a one-shot "approve
#    rest of session" — stays in-memory only.
#  - `reset_approve_all` (called by /clear): in-memory reset only;
#    leaves the on-disk preference intact.
_AUTO_ACCEPT_SETTING_KEY = "auto_accept_default"


def _auto_accept_settings_path():
    from pathlib import Path
    return Path.home() / ".alpha" / "settings.json"


def _load_auto_accept_default() -> bool:
    import json
    try:
        data = json.loads(_auto_accept_settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get(_AUTO_ACCEPT_SETTING_KEY, False)) if isinstance(data, dict) else False


def _persist_auto_accept(value: bool) -> None:
    import json
    path = _auto_accept_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, json.JSONDecodeError):
            data = {}
        data[_AUTO_ACCEPT_SETTING_KEY] = bool(value)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Read-only home or quota issue — don't crash the REPL over a preference.
        pass


_approve_all: bool = _load_auto_accept_default()


def reset_approve_all() -> None:
    """Reset the in-memory approve-all flag (called on /clear). Does NOT
    touch the persisted default — that's only changed by explicit
    set_auto_accept/toggle_auto_accept."""
    global _approve_all
    _approve_all = False


def is_auto_accept() -> bool:
    """Whether the session is currently auto-approving destructive tools."""
    return _approve_all


def set_auto_accept(value: bool) -> None:
    """Explicitly turn auto-accept on/off. Used by /accept-edits and shift+tab.
    Persists the choice to `~/.alpha/settings.json`."""
    global _approve_all
    _approve_all = bool(value)
    _persist_auto_accept(_approve_all)


def toggle_auto_accept() -> bool:
    """Flip auto-accept and return the new state. Persists to disk."""
    global _approve_all
    _approve_all = not _approve_all
    _persist_auto_accept(_approve_all)
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
                # Persists across REPL restarts. `/clear` clears it via
                # `reset_approve_all`; an explicit toggle (shift+tab,
                # /accept-edits) can turn it back off.
                set_auto_accept(True)
                print(f"  {c(C.GREEN + C.BOLD, '✓ Aprovado (all — salvo para futuras sessões)')}")
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


_LIVE_LABEL_MAX_VALUE = 40


def live_label_for_tool(name: str, args: dict) -> str:
    """Build the spinner label as `verb target` so the user can see WHICH
    file / command / task is in flight, not just the verb category.

    Falls back to the bare verb when args has no obviously informative key
    (avoids "Working {}" garbage)."""
    verb = label_for_tool(name)
    preview = _tool_args_preview(args, keys=_LIVE_LABEL_KEYS, limit=_LIVE_LABEL_MAX_VALUE)
    return f"{verb} {preview}" if preview else verb


