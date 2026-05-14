"""Smart loop detection for the agent main loop (#DM037).

Extracted from agent.py — detects when the LLM is stuck repeating itself
so the agent can force a final response instead of burning tokens.
"""

import json
from collections import Counter
from difflib import SequenceMatcher

from .config import LOOP_DETECTION as _LD

_MAX_REPEAT_CALLS = _LD["max_repeat_calls"]
_SIMILAR_REPEAT_CALLS = _LD["similar_repeat_calls"]
_SIMILARITY_THRESHOLD = _LD["similarity_threshold"]
_CYCLE_WINDOW = _LD["cycle_window"]
_STALE_WINDOW = _LD["stale_window"]
_LOOP_DETECT_MIN_ITER = _LD["min_iter"]
_LOOP_DETECT_MIN_CALLS = _LD["min_calls"]


def call_signature(tc: dict) -> str:
    """Create a deterministic signature normalizing JSON key order.

    Modelos que emitem chaves JSON em ordem variavel (DeepSeek, Ollama
    entre turns) produziriam assinaturas diferentes para chamadas
    logicamente identicas — quebrando loop detection. `sort_keys=True`
    garante que `{"a":1,"b":2}` e `{"b":2,"a":1}` colapsem
    na mesma signature.
    """
    raw = tc.get("arguments", "")
    try:
        args = json.loads(raw) if raw else {}
        normalized = json.dumps(args, sort_keys=True, ensure_ascii=False) if args else ""
    except (json.JSONDecodeError, TypeError):
        normalized = raw
    return f"{tc['name']}:{normalized}"


def result_preview(result: object, limit: int = 500) -> str:
    """Construir preview barato de tool result para `_recent_results`.

    Substitui `json.dumps(result, ensure_ascii=False, default=str)[:500]`
    que serializava 100KB+ inteiros so para descartar tudo apos 500 chars.
    """
    if not isinstance(result, dict):
        return str(result)[:limit]
    parts: list[str] = []
    remaining = limit
    for k, v in result.items():
        if remaining <= 0:
            break
        chunk = f"{k}={str(v)[:200]} "
        parts.append(chunk[:remaining])
        remaining -= len(chunk)
    return "".join(parts)[:limit]


def _parse_args_values(args_str: str) -> list[str]:
    """Extract individual argument values from JSON args for comparison."""
    try:
        args = json.loads(args_str)
        if isinstance(args, dict):
            return [str(v) for v in args.values()]
    except (json.JSONDecodeError, TypeError):
        pass
    return [args_str]


def _strip_common_prefix(va: str, vb: str) -> tuple[str, str]:
    """Drop the longest common prefix from two strings."""
    n = min(len(va), len(vb))
    i = 0
    while i < n and va[i] == vb[i]:
        i += 1
    return va[i:], vb[i:]


def _are_similar(sig_a: str, sig_b: str) -> bool:
    """Check if two call signatures are similar (same tool, same effective args)."""
    name_a, _, args_a = sig_a.partition(":")
    name_b, _, args_b = sig_b.partition(":")
    if name_a != name_b:
        return False
    if args_a == args_b:
        return True

    vals_a = _parse_args_values(args_a)
    vals_b = _parse_args_values(args_b)

    if len(vals_a) != len(vals_b):
        return False

    for va, vb in zip(vals_a, vals_b):
        if va == vb:
            continue
        ta, tb = _strip_common_prefix(va[:300], vb[:300])
        if not ta or not tb:
            continue
        ratio = SequenceMatcher(None, ta, tb).ratio()
        if ratio < _SIMILARITY_THRESHOLD:
            return False
    return True


def detect_cycle(calls: list[str]) -> bool:
    """Detect A→B→A→B style cycles in recent calls."""
    if len(calls) < 6:
        return False
    for cycle_len in range(2, len(calls) // 3 + 1):
        needed = cycle_len * 3
        if len(calls) < needed:
            continue
        recent = calls[-needed:]
        cycle = recent[:cycle_len]
        for rep in range(1, 3):
            segment = recent[rep * cycle_len : (rep + 1) * cycle_len]
            if segment != cycle:
                break
        else:
            return True
    return False


def detect_stale_progress(
    recent_results: list[str], window: int = _STALE_WINDOW
) -> bool:
    """Check if recent tool results are all very similar (no new info)."""
    if len(recent_results) < window:
        return False
    last_n = recent_results[-window:]
    pairs_similar = sum(
        1 for a, b in zip(last_n, last_n[1:])
        if SequenceMatcher(None, a[:500], b[:500]).ratio() > 0.90
    )
    return pairs_similar >= window - 2


def detect_loop(
    call_sigs: list[str],
    recent_calls: list[str],
    recent_results: list[str],
) -> str | None:
    """
    Smart loop detection. Returns a reason string if loop detected, None otherwise.

    Detects:
    1. Exact repetition (same call N times)
    2. Similar calls (same tool, similar args N times)
    3. A→B→A→B cycles
    4. Stale progress (results not changing)
    """
    # 1. Exact repetition
    counts = Counter(recent_calls)
    for sig in call_sigs:
        c = counts.get(sig, 0)
        if c >= _MAX_REPEAT_CALLS:
            return f"exact repeat: '{sig[:60]}' called {c}x"

    # 2. Similar calls — index by tool name first
    by_name: dict[str, list[str]] = {}
    for s in recent_calls:
        by_name.setdefault(s.partition(":")[0], []).append(s)
    for sig in call_sigs:
        candidates = by_name.get(sig.partition(":")[0])
        if not candidates or len(candidates) < _SIMILAR_REPEAT_CALLS:
            continue
        similar_count = sum(1 for s in candidates if _are_similar(sig, s))
        if similar_count >= _SIMILAR_REPEAT_CALLS:
            return f"similar calls: '{sig[:60]}' ~{similar_count}x"

    # 3. Cycle detection
    if detect_cycle(recent_calls):
        return "cycle detected in recent calls"

    # 4. Stale progress
    if detect_stale_progress(recent_results):
        return "stale progress — tool results not changing"

    return None
