"""
Markdown rendering for LLM responses.

Applies ANSI styling (code spans, bold, italic, headers, GFM tables) to a
finished Markdown block. Streaming inline rendering would need a state
machine across chunk boundaries, so this is batched end-of-turn.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

import re
import shutil
import textwrap

from .theme import NO_COLOR, C, c

# ─── Markdown regex ───
#
# Order matters: code spans first (so we never style `**bold**` inside `code`),
# then bold (must catch `**` before italic's `*`), then italic.
_MD_CODE_RE = re.compile(r"`([^`\n]+?)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<![\w*])\*([^*\n]+?)\*(?![\w*])")
_MD_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
# `※ recap:` is the session-end summary line documented in system.md.
# Captured pre-wrap so the body can be reflowed with hanging indent.
_MD_RECAP_RE = re.compile(r"^※ recap:\s*(.+?)\s*$", re.MULTILINE)


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
    # Format a markdown table as ASCII with navy separators, adapting
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


def _render_recap_line(body: str) -> str:
    """Wrap + style a `※ recap:` body. Prefix is cyan bold, body is dim italic.

    Hanging indent (2 chars) keeps the prefix visually anchored when the
    terminal wraps the body across multiple visual rows.
    """
    term_w = shutil.get_terminal_size((80, 24)).columns
    indent = "  "
    wrapped = textwrap.fill(
        body,
        width=max(40, term_w - 4),
        initial_indent="",
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return (
        f"{C.CYAN}{C.BOLD}※ recap:{C.RESET} "
        f"{C.DIM}{C.ITALIC}{wrapped}{C.RESET}"
    )


_MD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_MD_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(?=\S)", re.MULTILINE)
# Soft off-white for code: distinct from prose without shouting.
_CODE_FG = "\033[38;5;252m"


def render_markdown(text: str) -> str:
    # Apply ANSI styling (code, bold, italic, headers, tables, fenced code)
    # to a finished Markdown block. Batched end-of-turn — streaming inline
    # rendering would need a state machine across chunk boundaries.
    if NO_COLOR or not text:
        return text

    # Fenced code blocks are taken out first: their content must reach the
    # screen verbatim (a `*` or `|` inside code is not markup). The fence
    # lines themselves are dropped; the block is indented and tinted.
    out: list[str] = []
    prose: list[str] = []
    code: list[str] | None = None
    for line in text.split("\n"):
        if _MD_FENCE_RE.match(line):
            if code is None:
                if prose:
                    out.append(_render_prose("\n".join(prose)))
                    prose = []
                code = []
            else:
                out.extend(f"  {_CODE_FG}{ln}{C.RESET}" for ln in code)
                code = None
            continue
        (prose if code is None else code).append(line)
    if code is not None:  # unterminated fence — still show it as code
        out.extend(f"  {_CODE_FG}{ln}{C.RESET}" for ln in code)
    if prose:
        out.append(_render_prose("\n".join(prose)))
    return "\n".join(out)


def _render_prose(text: str) -> str:

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
        lambda m: f"{C.VIOLET_GLOW}{m.group(1)}{C.RESET}", rendered
    )
    rendered = _MD_BOLD_RE.sub(lambda m: f"{C.BOLD}{m.group(1)}{C.RESET}", rendered)
    rendered = _MD_ITALIC_RE.sub(lambda m: f"{C.ITALIC}{m.group(1)}{C.RESET}", rendered)
    rendered = _MD_HEADER_RE.sub(
        lambda m: f"{C.BOLD}{m.group(2)}{C.RESET}", rendered
    )
    rendered = _MD_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", rendered)
    # Recap LAST: body may include `code` and **bold** spans that have already
    # been styled by the rules above; we just wrap the prefix + body in the
    # session-recap look (cyan/bold prefix, dim italic remainder).
    rendered = _MD_RECAP_RE.sub(lambda m: _render_recap_line(m.group(1)), rendered)
    return rendered


# ─── ANSI-aware word wrap ───

_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_ANSI_SPLIT_RE = re.compile(r"(\x1b\[[0-9;]*m|\s+)")
_LIST_LEAD_RE = re.compile(r"^(\s*(?:•|\d+[.)])\s+)")


def visible_len(text: str) -> int:
    """Length of `text` as shown on screen (SGR escapes take no columns)."""
    return len(_ANSI_SGR_RE.sub("", text))


def wrap_ansi(line: str, width: int) -> list[str]:
    """Word-wrap one styled line to `width` visible columns.

    Continuation rows get a hanging indent matching the line's own lead
    (indentation plus a `• ` / `1. ` marker), and whatever styles were
    active at the break are closed on the row above and reopened on the
    row below — a bold phrase split in two stays bold, and nothing bleeds
    into the margin. Words longer than `width` are left to overflow.
    """
    if width < 10 or visible_len(line) <= width:
        return [line]
    plain = _ANSI_SGR_RE.sub("", line)
    lead = _LIST_LEAD_RE.match(plain)
    hang = " " * (len(lead.group(1)) if lead else len(plain) - len(plain.lstrip()))
    if len(hang) > width // 2:
        hang = ""

    rows: list[str] = []
    cur = ""
    cur_len = 0
    active: list[str] = []
    pending_space = ""
    for tok in _ANSI_SPLIT_RE.split(line):
        if not tok:
            continue
        if tok.startswith("\x1b["):
            cur += pending_space + tok
            cur_len += len(pending_space)
            pending_space = ""
            if tok in ("\x1b[0m", "\x1b[m"):
                active.clear()
            else:
                active.append(tok)
            continue
        if tok.isspace():
            # Leading indentation of the line is kept; other runs of
            # whitespace collapse to the break point or one gap.
            pending_space = tok if not cur else " "
            continue
        if cur_len and cur_len + len(pending_space) + len(tok) > width:
            rows.append(cur + ("\x1b[0m" if active else ""))
            cur = hang + "".join(active)
            cur_len = len(hang)
            pending_space = ""
        cur += pending_space + tok
        cur_len += len(pending_space) + len(tok)
        pending_space = ""
    if cur:
        rows.append(cur)
    return rows
