"""
Tool call rendering: header, result body, diffs, delegate aggregation,
spinner labels.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

import os
import re

from ..theme import (
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_MAX_LINES,
    DISPLAY_PREVIEW_TRUNCATE,
    NO_COLOR,
    ELBOW,
    RESULT_CONT,
    RESULT_INDENT,
    C,
    _SAFETY_COLORS,
    _truncate,
    c,
    fit,
    rel_path,
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
    "execute_shell": "Bash",
    "execute_pipeline": "Bash",
    "execute_python": "Python",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Update",
    "search_and_replace": "Replace",
    "list_directory": "List",
    "glob_files": "Glob",
    "search_files": "Search",
    "project_overview": "Overview",
    "web_search": "WebSearch",
    "web_fetch": "Fetch",
    "http_request": "HTTP",
    "git_operation": "Git",
    "run_tests": "Test",
    "todo_write": "Update Todos",
    "delegate_task": "Task",
    "delegate_parallel": "Tasks",
    "delegate_consensus": "Consensus",
    "ask_choice": "Ask",
    "present_plan": "Plan",
    "load_skill": "Skill",
    "tool_search": "ToolSearch",
    "record_memory": "Memory",
    "pre_flight": "Preflight",
    "agent_message": "Message",
    "agent_list": "Agents",
    "android_devices": "Phone",
    "android_screenshot": "PhoneScreen",
    "android_ui_dump": "PhoneUI",
    "android_input": "PhoneInput",
    "android_app": "PhoneApp",
    "android_logcat": "PhoneLog",
}


def _display_tool_name(name: str) -> str:
    alias = _DISPLAY_TOOL_NAME_ALIASES.get(name)
    if alias:
        return alias
    # `mcp__server__tool` → `server:tool` (the prefix is plumbing).
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}:{parts[2]}"
    return name


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
            if key in ("path", "file"):
                val = rel_path(val)
            break
    else:
        val = str(next(iter(args.values())))
    return _truncate(val, limit or DISPLAY_PREVIEW_TRUNCATE).replace("\n", " ")


def _read_file_preview(args: dict) -> str:
    """`read_file` often gets called multiple times against the same path
    with different offset/limit ranges. Showing only `path` makes the
    consecutive calls look identical and triggers the (×N) dedup falsely.
    Append the line range when present: `path:offset-(offset+limit)`."""
    path = rel_path(str(args.get("path", "")))
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


def _pattern_preview(args: dict) -> str:
    """`glob_files`/`search_files` carregam `pattern` + `path`. O preview
    generico prioriza `path` (vem antes em _HEADER_PREVIEW_KEYS), entao varias
    buscas com patterns diferentes na mesma pasta renderizam identicas e
    disparam o dedup (×N) falsamente. Mostra o pattern e o path so quando ele
    nao e o default '.'."""
    pattern = str(args.get("pattern", ""))
    path = rel_path(str(args.get("path", "") or ""))
    preview = pattern
    if path and path not in (".", "./"):
        preview = f"{pattern} in {path}" if pattern else path
    return _truncate(preview, DISPLAY_PREVIEW_TRUNCATE).replace("\n", " ")


def _android_input_preview(args: dict) -> str:
    """`tap 452,1065` / `swipe 100,900 → 100,300` / `text "abc"` / `key back`."""
    action = str(args.get("action", ""))
    if action in ("tap", "long_press"):
        return f"{action} {args.get('x')},{args.get('y')}"
    if action == "swipe":
        return f"swipe {args.get('x')},{args.get('y')} → {args.get('x2')},{args.get('y2')}"
    if action == "text":
        return f'text "{_strip_control(str(args.get("text", "")))}"'
    if action == "key":
        return f"key {args.get('key', '')}"
    return action


def _task_count(args: object) -> int:
    """Number of sub-agent tasks in a `delegate_parallel`-style call
    (`tasks` as a list or a JSON-encoded list), 0 when there is none."""
    if not isinstance(args, dict):
        return 0
    tasks = args.get("tasks")
    if isinstance(tasks, str):
        try:
            import json

            tasks = json.loads(tasks)
        except ValueError:
            return 0
    return len(tasks) if isinstance(tasks, list) else 0


def _format_tool_call_header(name: str, args: dict, safety: str) -> str:
    """Build `● ToolName(preview)` — shared by top-level and sub-agent
    rendering so the two visual variants never drift. The bullet carries
    the safety color (green = read-only, red = changes state); the name is
    plain bold so the transcript reads as text, not as a wall of color."""
    safety_color = _SAFETY_COLORS.get(safety, C.YELLOW)
    icon = c(safety_color, "●")
    shown = _display_tool_name(name)
    tool_name = c(C.BOLD, shown)
    if name == "read_file" and isinstance(args, dict):
        preview = _read_file_preview(args)
    elif name == "android_input" and isinstance(args, dict):
        preview = _android_input_preview(args)
    elif name == "android_app" and isinstance(args, dict):
        target = args.get("package") or rel_path(str(args.get("apk_path") or ""))
        preview = f"{args.get('action', '')} {target}".strip()
    elif name in ("android_ui_dump", "android_devices"):
        preview = ""
    elif name == "android_logcat" and isinstance(args, dict):
        preview = str(args.get("package") or args.get("grep") or "")
    elif _task_count(args):
        # A raw JSON list of prompts says nothing at a glance; each task
        # gets its own `Done …` row when the batch returns.
        preview = _plural(_task_count(args), "agent")
    elif isinstance(args, dict) and "pattern" in args:
        # Any search-like tool (glob_files, search_files, and future ones)
        # carries a `pattern` — show it instead of letting the generic preview
        # pick `path` first and render distinct searches identically.
        preview = _pattern_preview(args)
    else:
        preview = _tool_args_preview(args)
    # Keep the header on ONE row: 4 columns of bullet/indent + name + parens.
    preview = fit(preview, used=len(shown) + 6) if preview else ""
    paren = f"({preview})" if preview else ""
    return f"{icon} {tool_name}{paren}"


def print_tool_call(name: str, args: dict, safety: str = "safe") -> None:
    """Display a tool call as `● ToolName(args)`, flush left like the
    assistant's own `●` paragraphs, after a blank line of breathing room."""
    if name in _SELF_RENDERED_TOOLS:
        return
    print(f"\n{_format_tool_call_header(name, args, safety)}")


# Tools that draw their own UI (the menu IS the call and the answer) — a
# `● Ask(question)` header above it would only repeat the question.
_SELF_RENDERED_TOOLS = frozenset({"ask_choice"})

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


def _result_line(text: str, color: str = C.GRAY) -> str:
    """`  ⎿  text` — the first line of any tool result."""
    return f"{RESULT_INDENT}{c(C.GRAY_DARK, ELBOW)}  {c(color, text)}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _snippet_line_offset(path: str | None, new_text: str) -> int:
    """How many lines precede `new_text` in the file at `path` (0 when it
    can't be determined). `edit_file` diffs are computed on the replaced
    snippet alone; this turns snippet-relative numbers into real ones."""
    if not path or not new_text:
        return 0
    try:
        if os.path.getsize(path) > 2_000_000:
            return 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return 0
    idx = content.find(new_text)
    return content.count("\n", 0, idx) if idx > 0 else 0


def _render_diff(
    old_text: str,
    new_text: str,
    path: str | None = None,
    line_offset: int = 0,
) -> None:
    """Render an edit as `⎿  Updated path with N additions and M removals`
    followed by the numbered diff with full-width green/red rows.

    Line numbers come from each `@@` hunk header; the background is
    stretched to the right edge via `\\033[K` so highlighted rows don't
    end raggedly mid-line. Output is bounded by `_DIFF_MAX_LINES`."""
    import difflib

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    shown_path = rel_path(path) if path else ""

    diff = list(difflib.unified_diff(old_lines, new_lines, n=2, lineterm=""))
    if not diff:
        print(_result_line(f"{shown_path or 'file'}: no textual changes"))
        return

    body = [ln for ln in diff if not (ln.startswith("---") or ln.startswith("+++"))]
    added = sum(1 for ln in body if ln.startswith("+"))
    removed = sum(1 for ln in body if ln.startswith("-"))
    verb = "Updated" if old_text else "Wrote"
    target = f" {shown_path}" if shown_path else ""
    if old_text:
        counts = f"{_plural(added, 'addition')} and {_plural(removed, 'removal')}"
    else:
        counts = _plural(added, "line")
    print(
        f"{RESULT_INDENT}{c(C.GRAY_DARK, ELBOW)}  {c(C.GRAY, verb)}"
        f"{c(C.BOLD, target)} {c(C.GRAY, 'with ' + counts)}"
    )

    old_n = new_n = 0
    shown = 0
    hunks = 0
    pad = RESULT_CONT

    for line in body:
        if line.startswith("@@"):
            m = _DIFF_HUNK_RE.match(line)
            if m:
                old_n = int(m.group(1)) + line_offset
                new_n = int(m.group(2)) + line_offset
            # Hunk headers are noise next to real line numbers — a dim `⋮`
            # between hunks is enough to show the gap.
            if hunks:
                print(f"{pad}{c(C.GRAY_DARK, '   ⋮')}")
            hunks += 1
            continue

        if shown >= _DIFF_MAX_LINES:
            remaining = sum(1 for ln in body if not ln.startswith("@@")) - shown
            print(f"{pad}{c(C.GRAY, f'… +{remaining} more diff lines')}")
            break

        if line.startswith("+"):
            text = fit(line[1:], used=len(pad) + 8)
            print(_diff_line_full_width(
                pad, C.BG_GREEN, C.WHITE, f"{new_n:>4} + {text}",
            ))
            new_n += 1
        elif line.startswith("-"):
            text = fit(line[1:], used=len(pad) + 8)
            print(_diff_line_full_width(
                pad, C.BG_RED, C.WHITE, f"{old_n:>4} - {text}",
            ))
            old_n += 1
        else:
            text = fit(line[1:] if line.startswith(" ") else line, used=len(pad) + 8)
            print(f"{pad}{c(C.GRAY_DARK, f'{new_n:>4}')}   {c(C.GRAY, text)}")
            old_n += 1
            new_n += 1
        shown += 1


# #D316: neutraliza ANSI/C0/C1 (ESC, BEL, cursor moves, OSC 52 clipboard,
# limpar-tela) e overrides bidi de conteúdo de tool NÃO-confiável (web/MCP/
# stdout/read_file) antes de imprimir — evita terminal-injection/spoofing.
# Aplicado ao CONTEÚDO da tool, nunca à coloração legítima da UI.
_UNSAFE_DISPLAY_RE = re.compile(
    "[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f"
    "\\u200b-\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)


def _strip_control(text: str) -> str:
    return _UNSAFE_DISPLAY_RE.sub("", text)


def _print_result_body(lines: list[str], indent: str = RESULT_INDENT) -> None:
    """Render a result body: `  ⎿  first` then continuation lines aligned
    under the text, capped with `     … +N lines`. Lines are trimmed to the
    terminal width (never wrapped) and dimmed — tool output is supporting
    detail, the assistant's own words are the foreground."""
    if not lines:
        return
    elbow = c(C.GRAY_DARK, ELBOW)
    cont = " " * (len(indent) + 3)
    shown = min(len(lines), DISPLAY_MAX_LINES)
    for i, line in enumerate(lines[:shown]):
        text = c(C.GRAY, fit(_strip_control(line), used=len(cont)))
        if i == 0:
            print(f"{indent}{elbow}  {text}")
        else:
            print(f"{cont}{text}")
    if len(lines) > shown:
        remaining = len(lines) - shown
        more = "… +" + _plural(remaining, "line")
        print(f"{cont}{c(C.GRAY_DARK, more)}")


def _result_summary_line(result: dict) -> str:
    """Pick the most informative key for a result without printable output."""
    short = result.get("path") or str(result.get("count", ""))
    if not short and result.get("ok") is True:
        # Nothing printable but a success flag: say so instead of `True`.
        for key in ("message", "status", "summary"):
            if isinstance(result.get(key), str) and result[key].strip():
                return _truncate(result[key].strip(), DISPLAY_LINE_TRUNCATE - 10)
        return "Done"
    if not short:
        keys = [k for k in result if not k.startswith("_")]
        if keys:
            short = str(result[keys[0]])[:DISPLAY_LINE_TRUNCATE - 20]
    return _truncate(str(short), DISPLAY_LINE_TRUNCATE - 10)


_DELEGATE_TASK_MAX_STEPS = 5


def _print_delegate_step(step: object) -> None:
    """Render one step inside a Task block: `Tool: args_preview`."""
    if isinstance(step, dict):
        name = _display_tool_name(str(step.get("name", "?")))
        preview = str(step.get("args_preview", ""))
        text = f"{name}: {preview}" if preview else name
    else:
        text = str(step)
    text = _truncate(text, DISPLAY_PREVIEW_TRUNCATE)
    print(f"{RESULT_CONT}{c(C.GRAY, fit(text, used=len(RESULT_CONT)))}")


def _print_delegate_single(task_label: str, result: dict) -> None:
    """Render a single sub-agent result as an inline Task block."""
    steps = result.get("tools_used") or []
    iterations = result.get("iterations", len(steps))
    suffix = f" · {_plural(int(iterations or 0), 'step')}"
    short = fit(task_label.replace("\n", " "), used=len(RESULT_CONT) + len(suffix) + 6)
    print(
        f"{RESULT_INDENT}{c(C.GRAY_DARK, ELBOW)}  "
        f"{c(C.GRAY, 'Done')} {c(C.BOLD, short)}{c(C.GRAY, suffix)}"
    )
    shown = min(len(steps), _DELEGATE_TASK_MAX_STEPS)
    for step in steps[:shown]:
        _print_delegate_step(step)
    extra = len(steps) - shown
    if extra > 0:
        print(f"{RESULT_CONT}{c(C.GRAY_DARK, f'… +{extra} more')}")


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
    """Display a tool result under its header: `  ⎿  first line`, aligned
    continuation lines and a `… +N lines` footer. Tools whose raw output is
    bulk the user did not ask to read (file contents, listings, matches)
    collapse to a one-line summary instead."""
    if not isinstance(result, dict):
        _print_result_body([str(result)])
        return

    if name in ("delegate_task", "delegate_parallel") and not result.get("error"):
        _print_delegate_aggregate(name, result, args)
        return

    if result.get("error"):
        msg = fit(_strip_control(str(result["error"])).replace("\n", " "), used=12)
        print(_result_line(f"Error: {msg}", C.RED))
        return

    if name in _SELF_RENDERED_TOOLS:
        return

    if result.get("skipped"):
        reason = fit(str(result.get("reason", "denied")), used=5)
        print(_result_line(reason, C.YELLOW))
        return

    # Lazy import — thinking imports core, so a top-level import would loop.
    if name == "todo_write" and isinstance(result.get("todos"), list):
        todos = result["todos"]
        from ..thinking import get_active_indicator, set_pinned_todos
        ind = get_active_indicator()
        if ind is not None and ind.is_scroll_active():
            # Print the inline summary BEFORE pinning the panel: if the
            # panel size changes, `set_pinned_todos` triggers
            # teardown_scroll+setup_scroll, which leaves the cursor in the
            # reserved area at the bottom — a subsequent print() then
            # lands inside the panel rows and gets overwritten by the next
            # spinner redraw. Printing first keeps the line in normal
            # scroll-region territory.
            done = sum(1 for t in todos if t.get("status") == "completed")
            total = len(todos)
            print(_result_line(f"{_plural(total, 'todo')} · {done} completed"))
            set_pinned_todos(todos)
        else:
            _print_todo_list(todos)
        warning = result.get("warning")
        if warning:
            print(f"{RESULT_CONT}{c(C.YELLOW, '⚠ ' + str(warning))}")
        return

    # Diff rendering for edits — shows full +/- diff card, has its own framing.
    if name == "edit_file" and isinstance(args, dict) and args.get("old_text"):
        edit_path = str(result.get("path") or args.get("path") or "") or None
        new_text = str(args.get("new_text", ""))
        _render_diff(
            str(args.get("old_text", "")),
            new_text,
            edit_path,
            line_offset=_snippet_line_offset(edit_path, new_text),
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
        detail = f" ({_plural(int(n), 'occurrence')})" if n else ""
        verb = "Updated" if name == "edit_file" else "Wrote"
        print(_result_line(f"{verb} {rel_path(str(path))}{detail}"))
        return

    # Bulk-returning read-only tools: say WHAT came back, not the payload.
    summary = _bulk_summary(name, result)
    if summary:
        print(_result_line(summary))
        note = result.get("note") or result.get("warning")
        if isinstance(note, str) and note.strip():
            print(f"{RESULT_CONT}{c(C.GRAY_DARK, fit(note.strip(), used=len(RESULT_CONT)))}")
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
        # Trim blank lines only: leading spaces on the first line are data
        # (`git status --short` prints ` M path`).
        _print_result_body(output.strip("\n").rstrip().split("\n"))
    else:
        _print_result_body([_result_summary_line(result)])


def _bulk_summary(name: str, result: dict) -> str:
    """One-line summary for tools that return bulk the user rarely needs to
    read inline. Empty string = no special case, render the output."""
    if name == "read_file" and isinstance(result.get("content"), str):
        shown = len(result["content"].splitlines())
        total = result.get("total_lines")
        if isinstance(total, int) and total > shown:
            return f"Read {shown} of {_plural(total, 'line')}"
        return f"Read {_plural(shown, 'line')}"
    if name == "list_directory" and isinstance(result.get("entries"), list):
        count = result.get("count", len(result["entries"]))
        return f"Listed {_plural(int(count), 'path')}"
    if name == "search_files" and isinstance(result.get("results"), list):
        found = result.get("matches", len(result["results"]))
        return f"Found {found} match" + ("" if found == 1 else "es")
    if name == "glob_files":
        for key in ("files", "matches", "results"):
            val = result.get(key)
            if isinstance(val, list):
                return f"Found {_plural(len(val), 'file')}"
    if name == "android_ui_dump" and isinstance(result.get("nodes"), list):
        pkg = result.get("package") or "screen"
        return f"Found {_plural(int(result.get('count', 0)), 'element')} in {pkg}"
    if name == "android_logcat" and isinstance(result.get("lines"), list):
        return f"Read {_plural(len(result['lines']), 'log line')}"
    if name == "android_screenshot" and result.get("width"):
        return f"Captured {result['width']}x{result['height']}"
    if name == "android_devices" and isinstance(result.get("devices"), list):
        names = ", ".join(str(d.get("model") or d.get("serial")) for d in result["devices"])
        return f"{_plural(len(result['devices']), 'device')}: {names}"
    return ""


def label_for_tool(name: str) -> str:
    """Map a tool name to a short phase verb shown in the indicator."""
    if not name:
        return "Working"
    n = name.lower()
    if n.startswith("mcp__"):
        return "Calling MCP"
    if n.startswith("android_"):
        return "Phone"
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
    if _task_count(args):
        return f"{verb} {_plural(_task_count(args), 'agent')}"
    preview = _tool_args_preview(args, keys=_LIVE_LABEL_KEYS, limit=_LIVE_LABEL_MAX_VALUE)
    return f"{verb} {preview}" if preview else verb
