"""Loop detection for the Alpha agent.

Detects: exact repeats, similar calls, N-step cycles, stale progress.
Extracted from agent.py (#DM037).
"""

import json
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache

from ..config import LOOP_DETECTION as _LD

_MAX_REPEAT_CALLS = _LD["max_repeat_calls"]
_SIMILAR_REPEAT_CALLS = _LD["similar_repeat_calls"]
_SIMILARITY_THRESHOLD = _LD["similarity_threshold"]
_CYCLE_WINDOW = _LD["cycle_window"]
_STALE_WINDOW = _LD["stale_window"]
_LOOP_DETECT_MIN_ITER = _LD["min_iter"]
_LOOP_DETECT_MIN_CALLS = _LD["min_calls"]


def _call_signature(tc: dict) -> str:
    """Create a deterministic signature normalizing JSON key order.

    Modelos que emitem chaves JSON em ordem variavel (DeepSeek, Ollama
    entre turns) produziriam assinaturas diferentes para chamadas
    logicamente identicas — quebrando loop detection. `sort_keys=True`
    garante que `{\"a\":1,\"b\":2}` e `{\"b\":2,\"a\":1}` colapsem
    na mesma signature.
    """
    raw = tc.get("arguments", "")
    try:
        args = json.loads(raw) if raw else {}
        normalized = json.dumps(args, sort_keys=True, ensure_ascii=False) if args else ""
    except (json.JSONDecodeError, TypeError):
        normalized = raw
    return f"{tc['name']}:{normalized}"


def _result_preview(result: object, limit: int = 500) -> str:
    """Construir preview barato de tool result para `_recent_results`.

    Substitui `json.dumps(result, ensure_ascii=False, default=str)[:500]`
    que serializava 100KB+ inteiros so para descartar tudo apos 500 chars
    (#D023-PERF). Constroi a preview campo-a-campo cortando cada valor a
    200 chars e parando ao saturar `limit`. Resultado logico equivalente
    para deteccao de stale progress.
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


@lru_cache(maxsize=128)
def _parse_args_values(args_str: str) -> tuple[str, ...]:
    """Extract individual argument values from JSON args for comparison.

    DEEP_PERFORMANCE #D031: lru_cache evita re-parse de json.loads()
    nos mesmos args_str a cada comparação. Retorna tuple para hashability.
    """
    try:
        args = json.loads(args_str)
        if isinstance(args, dict):
            return tuple(str(v) for v in args.values())
    except (json.JSONDecodeError, TypeError):
        pass
    return (args_str,)


def _strip_common_prefix(va: str, vb: str) -> tuple[str, str]:
    """Drop the longest common prefix from two strings.

    Path-like args (e.g. ``/home/u/project/alpha`` vs ``/home/u/project/tests``)
    share a long prefix that dominates SequenceMatcher.ratio(), making distinct
    sibling paths look "similar" and triggering false-positive loop detection.
    Comparing only the differing tail collapses that bias.
    """
    n = min(len(va), len(vb))
    i = 0
    while i < n and va[i] == vb[i]:
        i += 1
    return va[i:], vb[i:]


def _are_similar(sig_a: str, sig_b: str) -> bool:
    """Check if two call signatures are similar (same tool, same effective args).

    Compares individual argument values with a path-prefix-aware ratio: the
    longest common prefix is stripped before measuring similarity, so sibling
    paths under the same root don't trip the threshold.
    """
    name_a, _, args_a = sig_a.partition(":")
    name_b, _, args_b = sig_b.partition(":")
    if name_a != name_b:
        return False
    if args_a == args_b:
        return True

    # Parse and compare individual argument values
    vals_a = _parse_args_values(args_a)
    vals_b = _parse_args_values(args_b)

    if len(vals_a) != len(vals_b):
        return False

    # All values must be similar for calls to be considered similar
    for va, vb in zip(vals_a, vals_b):
        if va == vb:
            continue
        # Strip shared prefix so two sibling paths don't look identical just
        # because they live under the same project root.
        ta, tb = _strip_common_prefix(va[:300], vb[:300])
        # Empty tails after stripping mean one is a prefix of the other —
        # treat as similar (same target, deeper/shallower view).
        if not ta or not tb:
            continue
        ratio = SequenceMatcher(None, ta, tb).ratio()
        if ratio < _SIMILARITY_THRESHOLD:
            return False
    return True


def _detect_cycle(calls: list[str]) -> bool:
    """Detect A→B→A→B style cycles in recent calls.

    Uses EXACT match only (not fuzzy) to avoid false positives with tools
    like execute_shell where different commands share similar structure.
    Requires at least 3 full cycle repetitions to confirm.
    """
    if len(calls) < 6:
        return False
    # Check for cycles of length 2 and 3, requiring 3 repetitions
    for cycle_len in (2, 3):
        needed = cycle_len * 3  # 3 full cycles
        if len(calls) < needed:
            continue
        recent = calls[-needed:]
        # Check if all 3 cycles are identical
        cycle = recent[:cycle_len]
        is_cycle = True
        for rep in range(1, 3):
            segment = recent[rep * cycle_len : (rep + 1) * cycle_len]
            if segment != cycle:
                is_cycle = False
                break
        if is_cycle:
            return True
    return False


def _quick_similar(a: str, b: str) -> bool:
    """Pre-filtro barato antes de SequenceMatcher.ratio().

    Evita o O(N²) do ratio() quando os resultados são obviamente
    idênticos (mesmo prefixo) ou obviamente diferentes (tamanhos
    dispares).
    """
    if abs(len(a) - len(b)) > max(100, int(max(len(a), len(b)) * 0.005)):
        return False
    if a[:100] == b[:100]:
        return True
    return SequenceMatcher(None, a[:500], b[:500]).ratio() > 0.90


def _detect_stale_progress(
    recent_results: list[str], window: int = _STALE_WINDOW
) -> bool:
    """Check if recent tool results are all very similar (no new info).

    AUDIT_V1.2 #023: comparing everything against ``last_n[0]`` missed stale
    progress when the sequence was e.g. [OK1, OK2, ERR3, ERR4, ERR5, ERR6]
    — ERR3-6 are similar to each other but NOT to OK1. Pairwise ``zip``
    comparison catches this: adjacent results that are all pairwise-similar
    indicate the agent is stuck in the same failure mode.
    """
    if len(recent_results) < window:
        return False
    last_n = recent_results[-window:]

    pairs_similar = sum(
        1 for a, b in zip(last_n, last_n[1:])
        if _quick_similar(a, b)
    )
    return pairs_similar >= window - 2  # allow 1 transition


_ERROR_KEYWORDS = (
    "error", "denied", "blocked", "não permitida", "nao permitida",
    "not defined", "exception", "traceback", "is not defined",
    "permission",
)
_ERROR_STREAK_THRESHOLD = _LD.get("error_streak_threshold", 4)


def _detect_error_streak(recent_results: list[str]) -> int:
    """Count consecutive error-like results at the tail. A long streak means
    the agent is thrashing on something that won't start working — the four
    existing detectors miss this because each retry uses a different tool or
    different args. Returning the streak length lets the caller decide.
    """
    streak = 0
    for r in reversed(recent_results):
        rl = r.lower()
        if any(kw in rl for kw in _ERROR_KEYWORDS):
            streak += 1
        else:
            break
    return streak


def _detect_loop(
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
    5. Error streak (N consecutive tool failures — "frustration loop")
    """
    # 1. Exact repetition — Counter em vez de N x list.count() (O(N) vs O(N*M))
    counts = Counter(recent_calls)
    for sig in call_sigs:
        c = counts.get(sig, 0)
        if c >= _MAX_REPEAT_CALLS:
            return f"exact repeat: '{sig[:60]}' called {c}x"

    # 2. Similar calls (same tool with slightly different args) — indexa por
    # tool name primeiro para evitar SequenceMatcher quando os nomes diferem.
    # Em sessoes ativas com ~60 recent_calls e 5+ tools diferentes, isso
    # corta ~80% das comparacoes caras.
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

    # 3. Cycle detection (A→B→A→B)
    if _detect_cycle(recent_calls):
        return "cycle detected in recent calls"

    # 4. Stale progress
    if _detect_stale_progress(recent_results):
        return "stale progress — tool results not changing"

    # 5. Frustration loop — back-to-back tool failures
    streak = _detect_error_streak(recent_results)
    if streak >= _ERROR_STREAK_THRESHOLD:
        return f"error streak: {streak} consecutive failed tool calls"

    return None


