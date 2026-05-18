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
