"""
Cross-session memory (Plano-Upgrade-v3 §3.1 / H2 #10).

A long-lived markdown scratchpad the agent can write to and read from
across sessions. Closes the "every chat starts cold" gap that Skills
alone don't fill — Skills are static distribution artifacts; this is
adaptive, user-and-workspace specific knowledge.

## Storage

`~/.alpha/memory/<scope>.md`. Two scopes today:
- `global` — preferences, language conventions, recurring user feedback
- `workspace` — auto-derived from current cwd; project-specific notes

Format is plain Markdown, append-only headers per date. Users can
hand-edit (`/memory edit` opens $EDITOR; falling back to nano/vi).

## Size cap

8 KB per file. When exceeded, the oldest entries are dropped on the
next `record()` call. Memory is meant to be relevant snippets, not a
log — anything that grows unbounded belongs in history.py or a real db.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from .settings import alpha_user_dir

logger = logging.getLogger(__name__)


MEMORY_DIR_NAME = "memory"
MAX_MEMORY_BYTES = 8192

Scope = Literal["global", "workspace"]
VALID_SCOPES: tuple[Scope, ...] = ("global", "workspace")

# Header format: `## YYYY-MM-DD HH:MM (kind)` — kind is freeform but
# typically one of: preference, pattern, fix, note. The kind helps the
# agent filter relevant entries when reading.
_HEADER_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) \((\w+)\)$", re.MULTILINE)


def _memory_root() -> Path:
    return alpha_user_dir(MEMORY_DIR_NAME)


def _workspace_token() -> str:
    """Stable short token derived from cwd basename for filename use.

    Sanitizes to `[a-zA-Z0-9_.-]`, caps at 64 chars, falls back to
    'default' when cwd has no usable basename (e.g. '/').

    Test isolation: tests monkeypatch `alpha.memory.Path.home` to redirect
    the storage tree. Tests that need a different *workspace identity*
    can `monkeypatch.chdir(tmp_path)` — same primitive the real REPL uses."""
    base = os.path.basename(os.path.normpath(os.getcwd()))
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", base) or "default"
    return safe[:64]


def _path_for(scope: Scope) -> Path:
    root = _memory_root()
    if scope == "global":
        return root / "global.md"
    if scope == "workspace":
        return root / f"workspace-{_workspace_token()}.md"
    raise ValueError(f"unknown memory scope: {scope!r}. Use one of {VALID_SCOPES}")


def _read(scope: Scope) -> str:
    p = _path_for(scope)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("failed to read memory %s: %s", p, e)
        return ""


def _write(scope: Scope, content: str) -> Path:
    p = _path_for(scope)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        logger.warning("failed to write memory %s: %s", p, e)
    return p


def _trim_to_cap(content: str, cap_bytes: int | None = None) -> str:
    """Drop oldest entries until the file fits the cap.

    Entries are delimited by `## YYYY-MM-DD ...` headers; an entry runs
    from one header to the next. If the file has no headers (legacy or
    user-edited free-form text), truncate from the start as a last
    resort. Always keeps at least the most recent entry."""
    # Resolve cap at call time (not at def time) so monkeypatching
    # MAX_MEMORY_BYTES in tests actually takes effect.
    if cap_bytes is None:
        cap_bytes = MAX_MEMORY_BYTES
    encoded = content.encode("utf-8")
    if len(encoded) <= cap_bytes:
        return content

    matches = list(_HEADER_RE.finditer(content))
    if len(matches) <= 1:
        # No way to split cleanly — truncate from the front, leaving the
        # last cap_bytes worth of text. Tag the truncation so users see it.
        tail = encoded[-cap_bytes:].decode("utf-8", errors="replace")
        return f"<!-- memory truncated -->\n{tail}"

    # Drop entries from the start until we fit.
    for i in range(1, len(matches)):
        candidate = content[matches[i].start():]
        if len(candidate.encode("utf-8")) <= cap_bytes:
            return candidate
    # Only one entry remains; keep it even if it's bigger than the cap.
    return content[matches[-1].start():]


def record(
    content: str,
    *,
    scope: Scope = "workspace",
    kind: str = "note",
    now: datetime | None = None,
) -> dict:
    """Append a new memory entry. Trims oldest if over cap.

    Returns {"ok": True, "path": str, "trimmed": bool} on success."""
    if not isinstance(content, str) or not content.strip():
        return {"ok": False, "error": "content must be a non-empty string"}
    if scope not in VALID_SCOPES:
        return {"ok": False, "error": f"scope must be one of {VALID_SCOPES}"}
    kind = re.sub(r"[^a-zA-Z]", "", kind)[:32] or "note"

    existing = _read(scope)
    ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    entry = f"## {ts} ({kind})\n{content.strip()}\n\n"
    new_content = existing + entry

    pre_trim_len = len(new_content.encode("utf-8"))
    new_content = _trim_to_cap(new_content)
    trimmed = len(new_content.encode("utf-8")) < pre_trim_len

    path = _write(scope, new_content)
    return {"ok": True, "path": str(path), "trimmed": trimmed, "kind": kind}


def list_entries(scope: Scope = "workspace") -> list[dict]:
    """Parse the memory file into structured entries (newest first)."""
    content = _read(scope)
    if not content:
        return []
    matches = list(_HEADER_RE.finditer(content))
    if not matches:
        return [{"ts": "?", "kind": "raw", "body": content.strip()}]

    out: list[dict] = []
    for i, m in enumerate(matches):
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()
        out.append({"ts": m.group(1), "kind": m.group(2), "body": body})
    out.reverse()  # newest first
    return out


def forget(index: int, *, scope: Scope = "workspace") -> dict:
    """Drop the entry at 1-based `index` (newest=1).

    Returns {"ok": True, "removed": {...}} or {"ok": False, "error": ...}."""
    entries = list_entries(scope)
    if not entries:
        return {"ok": False, "error": "no memory entries to forget"}
    if not isinstance(index, int) or index < 1 or index > len(entries):
        return {"ok": False, "error": f"index out of range (1..{len(entries)})"}

    removed = entries.pop(index - 1)
    # Rewrite oldest→newest so headers stay chronological in the file.
    entries.reverse()
    rebuilt = "".join(
        f"## {e['ts']} ({e['kind']})\n{e['body']}\n\n" for e in entries
    )
    _write(scope, rebuilt)
    return {"ok": True, "removed": removed}


def clear(scope: Scope = "workspace") -> dict:
    """Wipe the memory file for a scope. Returns count of removed entries."""
    entries = list_entries(scope)
    _write(scope, "")
    return {"ok": True, "removed_count": len(entries)}


def memory_path(scope: Scope = "workspace") -> Path:
    """Public accessor — used by /memory edit to know where to open $EDITOR."""
    return _path_for(scope)


def summary_for_prompt(max_chars: int = 4000) -> str:
    """Build a concise block to inject into the system prompt.

    Concatenates global + workspace memories, newest first, capped at
    `max_chars`. Returns "" when nothing relevant is recorded — callers
    can skip the prompt section entirely in that case."""
    pieces: list[str] = []
    for scope_name in ("global", "workspace"):
        entries = list_entries(scope_name)
        if not entries:
            continue
        header = "Global memory:" if scope_name == "global" else "This workspace:"
        lines = [header]
        for e in entries:
            lines.append(f"- ({e['kind']}, {e['ts']}) {e['body']}")
        pieces.append("\n".join(lines))
    text = "\n\n".join(pieces)
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text
