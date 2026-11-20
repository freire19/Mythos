"""Tool lazy-loading (parity wave 2026-11, item 2).

Problem: every prompt carried the full schema of ~70 built-in tools plus
every MCP tool (~9.4K tokens, 31 of them `browser_*`). Most turns use a
handful. Mirrors Claude Code's "deferred tools + ToolSearch": heavy or
rarely used tools are *deferred* — dropped from the `tools` array and
listed by name (one line each) in the description of a SAFE `tool_search`
tool. The model calls `tool_search("select:a,b")` or `tool_search("<kw>")`
to pull the full schemas in; from the next iteration on those tools ride
along in the `tools` array for the rest of the session.

Design points:

- **Lenient dispatch.** A deferred tool the model calls WITHOUT loading
  still executes — the registry, approval gate and plan-mode deny all stay
  unchanged. Lazy-loading only controls what the *prompt* carries, never
  what can run. So a misguessed call costs one error round-trip, not a
  hard failure.
- **Applied per iteration, on the already-filtered list.** `apply()` runs
  inside `run_agent` right before each LLM call, on whatever `tools` the
  caller passed (agent-profile filter, plan-mode filter, sub-agent
  blocklist…). It can only *remove* from that list, and only *add back*
  entries that were in it. It never widens scope.
- **Session-global loaded set.** The REPL recreates the event loop every
  turn (`asyncio.run`), so a ContextVar would forget loads between turns.
  A module-level set is the right lifetime: loaded once, visible until
  `/clear` (`reset_loaded`). Sub-agents sharing it is harmless — it only
  adds schemas they were already allowed to see.
- **Kill switch** `ALPHA_LAZY_TOOLS=off` restores the old all-schemas
  prompt. `ALPHA_LAZY_MCP_THRESHOLD` (default 8) keeps small MCP servers
  inline; bigger ones are deferred as a whole.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable

logger = logging.getLogger(__name__)

TOOL_SEARCH_NAME = "tool_search"

# Categories deferred wholesale. Browser alone is 31 schemas.
_DEFERRED_CATEGORIES: frozenset[str] = frozenset({"browser", "scraping", "database", "android"})

# Individual built-ins that are rarely needed in a coding session.
_DEFERRED_NAMES: frozenset[str] = frozenset({
    "google_antigravity",
    "clipboard_read",
    "clipboard_write",
    "deploy_check",
    "install_package",
})

# Hard cap on how many schemas one search returns (keeps the tool result
# small; the model can search again with a narrower query).
MAX_SEARCH_RESULTS = 8
# Chars of description shown per deferred tool in the index line.
_INDEX_DESC_CHARS = 70

_loaded: set[str] = set()
_lock = threading.Lock()


# ── enablement ───────────────────────────────────────────────────────


def enabled() -> bool:
    from .config import lazy_tools_enabled
    return lazy_tools_enabled()


def _mcp_threshold() -> int:
    from .config import lazy_mcp_threshold
    return lazy_mcp_threshold()


# ── classification ───────────────────────────────────────────────────


def _category_str(td) -> str:
    cat = getattr(td, "category", "") or ""
    return str(getattr(cat, "value", cat)).lower()


def _mcp_tool_count() -> int:
    from .tools import TOOL_REGISTRY
    return sum(1 for td in TOOL_REGISTRY.values() if _category_str(td).startswith("mcp:"))


def is_deferred(name: str, td=None) -> bool:
    """Whether `name` is a deferred tool (schema held back until searched).

    `td` is the `ToolDefinition` when the caller already has it; otherwise
    it is resolved from the registry. Unknown names are never deferred —
    the caller's own filtering handles those.
    """
    if name == TOOL_SEARCH_NAME:
        return False
    if name in _DEFERRED_NAMES:
        return True
    if td is None:
        from .tools import get_tool
        td = get_tool(name)
        if td is None:
            return False
    cat = _category_str(td)
    if cat in _DEFERRED_CATEGORIES:
        return True
    if cat.startswith("mcp:"):
        return _mcp_tool_count() > _mcp_threshold()
    return False


# ── loaded set ───────────────────────────────────────────────────────


def loaded_names() -> frozenset[str]:
    with _lock:
        return frozenset(_loaded)


def mark_loaded(names) -> None:
    with _lock:
        _loaded.update(n for n in names if isinstance(n, str) and n)


def reset_loaded() -> None:
    """Forget every loaded tool (called by `/clear`)."""
    with _lock:
        _loaded.clear()


# ── prompt-side application ─────────────────────────────────────────


def _name_of(entry: dict) -> str | None:
    fn = entry.get("function") if isinstance(entry, dict) else None
    name = (fn or {}).get("name") if isinstance(fn, dict) else None
    return name if isinstance(name, str) and name else None


def _one_line(desc: str, limit: int = _INDEX_DESC_CHARS) -> str:
    first = (desc or "").strip().split("\n", 1)[0].strip()
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    return first


def build_tool_search_schema(deferred: list[dict]) -> dict:
    """OpenAI-format schema for `tool_search`, whose description doubles as
    the index of currently deferred tools (name + one-line description)."""
    lines = []
    for entry in deferred:
        fn = entry.get("function", {})
        lines.append(f"- {fn.get('name')}: {_one_line(fn.get('description', ''))}")
    index = "\n".join(lines) if lines else "(none)"
    description = (
        "Load the full schema of DEFERRED tools so you can call them. "
        "Deferred tools are available but their parameters are not in your "
        "tool list yet — call tool_search FIRST, then call the tool on the "
        "next step. Query forms: `select:name1,name2` for exact names, or "
        "free keywords matched against name + description. Load every tool "
        "you expect to need in ONE call.\n\n"
        f"Deferred tools:\n{index}"
    )
    return {
        "type": "function",
        "function": {
            "name": TOOL_SEARCH_NAME,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "`select:a,b,c` for exact tool names, or keywords "
                            "(e.g. 'browser click fill', 'database tables')."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Max schemas to return (default 5, cap {MAX_SEARCH_RESULTS}).",
                    },
                },
                "required": ["query"],
            },
        },
    }


def apply(tools: list[dict], get_tool_fn: Callable | None = None) -> list[dict]:
    """Return the prompt-side tool list: non-deferred tools + already loaded
    deferred tools + a `tool_search` entry indexing the rest.

    Pure with respect to `tools` (never mutates it). Returns `tools`
    unchanged when lazy-loading is off or nothing in the list is deferred.
    """
    if not tools or not enabled():
        return tools
    lookup = get_tool_fn
    if lookup is None:
        from .tools import get_tool
        lookup = get_tool
    loaded = loaded_names()
    kept: list[dict] = []
    deferred: list[dict] = []
    for entry in tools:
        name = _name_of(entry)
        if name is None or name == TOOL_SEARCH_NAME:
            continue
        td = lookup(name) if lookup else None
        if td is not None and is_deferred(name, td) and name not in loaded:
            deferred.append(entry)
        else:
            kept.append(entry)
    if not deferred and not any(
        (lookup(_name_of(e)) is not None and is_deferred(_name_of(e), lookup(_name_of(e))))
        for e in kept
    ):
        return tools
    kept.append(build_tool_search_schema(deferred))
    return kept


# ── search ───────────────────────────────────────────────────────────

_SELECT_RE = re.compile(r"^\s*select\s*:\s*(.+)$", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _stem(tok: str) -> str:
    """Tiny plural folding so "tables" matches "table" (and vice versa)."""
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _candidates() -> list[dict]:
    """Every deferred tool currently advertisable (respects enable flags)."""
    from .tools import get_openai_tools, get_tool
    out = []
    for entry in get_openai_tools():
        name = _name_of(entry)
        if name and is_deferred(name, get_tool(name)):
            out.append(entry)
    return out


def search(query: str, max_results: int | None = None) -> dict:
    """Resolve `query` against the deferred tools, mark hits as loaded and
    return their full schemas. Shape: `{ok, loaded: [...], tools: [...],
    not_found: [...]}`."""
    limit = MAX_SEARCH_RESULTS if not max_results else max(1, min(int(max_results), MAX_SEARCH_RESULTS))
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query vazia — use `select:nome1,nome2` ou palavras-chave."}

    cands = _candidates()
    by_name = {_name_of(e): e for e in cands}

    m = _SELECT_RE.match(q)
    hits: list[dict] = []
    not_found: list[str] = []
    if m:
        wanted = [w.strip() for w in m.group(1).split(",") if w.strip()]
        for w in wanted:
            entry = by_name.get(w)
            if entry is None:
                # Exact name outside the deferred set: say so (it may simply
                # already be in the tool list).
                not_found.append(w)
            else:
                hits.append(entry)
        hits = hits[:limit]
    else:
        from .tools import get_tool
        terms = {_stem(t) for t in _TOKEN_RE.findall(q.lower())}
        scored: list[tuple[int, int, str, dict]] = []
        for i, entry in enumerate(cands):
            fn = entry["function"]
            name = fn.get("name", "").lower()
            desc = (fn.get("description") or "").lower()
            hay_name = {_stem(t) for t in _TOKEN_RE.findall(name)}
            # Category tokens ("database", "browser", "mcp:<server>") let a
            # query about the *family* rank every member above tools that
            # merely share one word (browser_query vs "database query").
            td = get_tool(name)
            hay_cat = {_stem(t) for t in _TOKEN_RE.findall(_category_str(td))} if td else set()
            hay_desc = {_stem(t) for t in _TOKEN_RE.findall(desc)}
            score = 0
            for t in terms:
                if t in hay_name:
                    score += 3
                elif t in hay_cat:
                    score += 2
                elif t in name:
                    score += 2
                elif t in hay_desc:
                    score += 1
            if score:
                scored.append((-score, i, name, entry))
        scored.sort()
        hits = [e for _, _, _, e in scored[:limit]]

    names = [_name_of(e) for e in hits]
    mark_loaded(names)
    if names:
        logger.info("tool_search loaded %d tool(s): %s", len(names), ", ".join(names))
    result = {
        "ok": True,
        "loaded": names,
        "tools": hits,
        "hint": (
            "Schemas above are now in your tool list — call them directly on the next step."
            if names else
            "Nenhuma tool diferida casou com a busca. Tente `select:nome` ou outras palavras."
        ),
    }
    if not_found:
        result["not_found"] = not_found
    return result
