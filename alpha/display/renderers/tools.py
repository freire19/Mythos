"""
Tool call rendering: header, result body, diffs, delegate aggregation,
spinner labels.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

import re

from ..theme import (
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_MAX_LINES,
    DISPLAY_PREVIEW_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    NO_COLOR,
    C,
    _SAFETY_COLORS,
    _SAFETY_ICONS,
    _truncate,
    c,
)
from .planning import _print_todo_list

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
        from ..thinking import get_active_indicator, set_pinned_todos
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
