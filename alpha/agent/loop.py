"""Loop detection for the Alpha agent.

Detects: exact repeats, similar calls, N-step cycles, stale progress.
Extracted from agent.py (#DM037).
"""

from __future__ import annotations

import json
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache

from ..config import LOOP_DETECTION as _LD

_MAX_REPEAT_CALLS = _LD["max_repeat_calls"]
# #015: the current-turn exact-repeat check (`_MAX_REPEAT_CALLS`) means "the
# model is re-issuing a saturated call RIGHT NOW" — a strong signal at a low
# threshold (3). The historical-only check (a sig saturated in the window but
# NOT in the current turn) is decoupled from "right now", so it uses a
# STRICTER threshold (2×). Otherwise a routine pattern — re-reading the same
# file 3× between edits, then doing 50 calls of real work — would force-final a
# productive session just because the old sig still lingers in the window.
_HISTORICAL_REPEAT_THRESHOLD = _MAX_REPEAT_CALLS * 2
_SIMILAR_REPEAT_CALLS = _LD["similar_repeat_calls"]
_SIMILARITY_THRESHOLD = _LD["similarity_threshold"]
_CYCLE_WINDOW = _LD["cycle_window"]
_STALE_WINDOW = _LD["stale_window"]
_LOOP_DETECT_MIN_ITER = _LD["min_iter"]
_LOOP_DETECT_MIN_CALLS = _LD["min_calls"]

# Caps for `_call_signature`: args payloads beyond `_SIG_RAW_CAP` skip the
# JSON round-trip entirely; the stored signature keeps at most
# `_SIG_ARGS_CAP` chars of args, prefixed with the full length so
# different-length payloads never collide.
_SIG_RAW_CAP = 8192
_SIG_ARGS_CAP = 2048


def _call_signature(tc: dict) -> str:
    """Create a deterministic signature normalizing JSON key order.

    Modelos que emitem chaves JSON em ordem variavel (DeepSeek, Ollama
    entre turns) produziriam assinaturas diferentes para chamadas
    logicamente identicas — quebrando loop detection. `sort_keys=True`
    garante que `{\"a\":1,\"b\":2}` e `{\"b\":2,\"a\":1}` colapsem
    na mesma signature.
    """
    name = tc.get("name", "unknown")  # .get() → truncated stream safety (#007)
    raw = tc.get("arguments", "")
    if isinstance(raw, str) and len(raw) > _SIG_RAW_CAP:
        return f"{name}:{len(raw)}:{raw[:_SIG_ARGS_CAP]}"
    try:
        args = json.loads(raw) if raw else {}
        normalized = json.dumps(args, sort_keys=True, ensure_ascii=False) if args else ""
    except (json.JSONDecodeError, TypeError):
        normalized = raw
    if isinstance(normalized, str) and len(normalized) > _SIG_ARGS_CAP:
        return f"{name}:{len(normalized)}:{normalized[:_SIG_ARGS_CAP]}"
    return f"{name}:{normalized}"


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


def _pair_similar(va: str, vb: str) -> bool:
    """Single pair-of-argument-values similarity rule (#DM065).

    Shared by `_are_similar` (ALL pairs similar) and `_args_distinct`
    (ANY pair NOT similar) so the per-pair rule can't drift between them.

    A pair is similar when:
    - the values are equal; or
    - after stripping the longest common prefix (sibling paths under the
      same project root shouldn't look identical), one side is empty —
      i.e. one value is a prefix of the other (same target, deeper/
      shallower view); or
    - the stripped tails pass the cheap length gate (#DP034: length delta
      beyond what the threshold could ever accept short-circuits the
      O(N²) ratio) AND `SequenceMatcher.ratio() >= _SIMILARITY_THRESHOLD`.
    """
    if va == vb:
        return True
    ta, tb = _strip_common_prefix(va[:300], vb[:300])
    if not ta or not tb:
        return True
    if abs(len(ta) - len(tb)) > (1 - _SIMILARITY_THRESHOLD) * max(len(ta), len(tb)):
        return False
    return SequenceMatcher(None, ta, tb).ratio() >= _SIMILARITY_THRESHOLD


def _are_similar(sig_a: str, sig_b: str) -> bool:
    """Check if two call signatures are similar (same tool, same effective args).

    Compares individual argument values with a path-prefix-aware ratio (see
    `_pair_similar`): the longest common prefix is stripped before measuring
    similarity, so sibling paths under the same root don't trip the threshold.
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
    return all(_pair_similar(va, vb) for va, vb in zip(vals_a, vals_b))


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


# #DM065 (nota): ratio do pré-filtro de result-previews. Deliberadamente
# separado de `_SIMILARITY_THRESHOLD` (que compara ARGS de chamadas) — este
# compara previews de RESULTADOS truncados a 500 chars; antes era um `0.90`
# hardcodado no meio de `_quick_similar`.
_QUICK_SIMILAR_RATIO = 0.90


def _quick_similar(a: str, b: str) -> bool:
    """Pre-filtro barato antes de SequenceMatcher.ratio().

    Evita o O(N²) do ratio() quando os resultados são obviamente
    idênticos (mesmo prefixo) ou obviamente diferentes (tamanhos
    dispares).
    """
    # #D113: dois previews vazios ("" == "") nao significam "stale" — vem de
    # tools que retornam resultado vazio/None. Sem este guard, uma sequencia
    # de resultados vazios contaria como progresso estagnado e poderia forcar
    # um final-response prematuro.
    if not a and not b:
        return False
    if abs(len(a) - len(b)) > max(100, int(max(len(a), len(b)) * 0.005)):
        return False
    if a[:100] == b[:100]:
        return True
    return SequenceMatcher(None, a[:500], b[:500]).ratio() > _QUICK_SIMILAR_RATIO


def _args_distinct(sig_a: str, sig_b: str) -> bool:
    """Mirror of `_are_similar` — returns True when args target clearly
    different things (different files, different paths, etc.).

    Used by `_detect_stale_progress` to suppress false positives when the
    agent is reading N distinct files that share a header/template format:
    the result *previews* look alike (all start with `# DEEP_X — ...`),
    but each call legitimately advances exploration. Without this guard,
    reading 6 different audit files would trip stale detection just
    because they share a markdown template.

    `_are_similar` requires ALL value-pairs to look alike (`_pair_similar`);
    here we return True if ANY pair is clearly distinct (NOT similar) after
    stripping a shared prefix — same per-pair rule, mirrored polarity.
    """
    name_a, _, args_a = sig_a.partition(":")
    name_b, _, args_b = sig_b.partition(":")
    if name_a != name_b:
        # Different tools — conservatively NOT flagging as distinct, so we
        # don't suppress stale-detection across tool-name flips (which is
        # exactly the cycle-like case the other detectors care about).
        return False
    if args_a == args_b:
        return False

    vals_a = _parse_args_values(args_a)
    vals_b = _parse_args_values(args_b)
    if len(vals_a) != len(vals_b):
        return True

    return any(not _pair_similar(va, vb) for va, vb in zip(vals_a, vals_b))


def _detect_stale_progress(
    recent_results: list[str],
    recent_calls: list[str] | None = None,
    window: int = _STALE_WINDOW,
    pending: int = 0,
) -> bool:
    """Check if recent tool results are all very similar (no new info).

    AUDIT_V1.2 #023: comparing everything against ``last_n[0]`` missed stale
    progress when the sequence was e.g. [OK1, OK2, ERR3, ERR4, ERR5, ERR6]
    — ERR3-6 are similar to each other but NOT to OK1. Pairwise ``zip``
    comparison catches this: adjacent results that are all pairwise-similar
    indicate the agent is stuck in the same failure mode.

    When `recent_calls` is provided, adjacent pairs whose *calls* clearly
    target distinct args (different files, etc.) don't count as stale,
    even if their result previews look alike — that's a template effect,
    not lack of progress. Fixes the "reading N audit files looks stale
    because the markdown headers are identical" false-positive.

    AUDIT_V2.3 #203: the caller extends the call list eagerly at the start
    of each iteration — the CURRENT batch (`pending` entries, still
    unexecuted) sits at the tail of `recent_calls` with no matching
    results. Slicing `recent_calls[-len(recent_results):]` kept that
    pending batch and dropped the oldest executed calls, so every
    `aligned[i]` actually matched `recent_results[i + batch]` — the
    args-distinct rescue compared the WRONG pairs. The pending count
    cannot be reconstructed from the two lists (both saturate at their
    caps in steady state — the length difference goes to zero), so the
    caller passes it explicitly. After dropping the pending tail, the
    remaining `executed` list and `recent_results` are tail-synchronized
    (both keep the most-recent entries), so the last `window` of each
    align 1:1.
    """
    if len(recent_results) < window:
        return False
    last_n = recent_results[-window:]

    last_calls: list[str] | None = None
    if recent_calls is not None:
        executed = (
            recent_calls[: max(0, len(recent_calls) - pending)]
            if pending
            else recent_calls
        )
        if len(executed) >= window:
            last_calls = executed[-window:]

    target = window - 2  # allow 1 transition
    pairs_similar = 0
    for i, (a, b) in enumerate(zip(last_n, last_n[1:])):
        if not _quick_similar(a, b):
            continue
        if last_calls is not None and _args_distinct(last_calls[i], last_calls[i + 1]):
            # Result previews look alike (template effect), but calls
            # targeted clearly different inputs — real progress.
            continue
        pairs_similar += 1
        if pairs_similar >= target:
            return True
        # Early-exit: remaining pairs can't reach target
        remaining = (window - 1) - (i + 1)
        if pairs_similar + remaining < target:
            return False
    return pairs_similar >= target


_ERROR_STREAK_THRESHOLD = _LD.get("error_streak_threshold", 4)


def _result_is_failure(result: object) -> bool:
    """Classify a tool result as a failure for the error-streak detector.

    #D104: the original heuristic matched generic substrings ("error",
    "exception", "traceback", "permission") anywhere in the *string preview*,
    so four consecutive reads of files that merely *contain* those words —
    e.g. auditing error-handling code, like this very file — tripped a
    spurious streak and cut the agent off. A later attempt keyed off a
    `ok=false` substring in the lossy preview, which both (a) re-introduced
    the same false positive for content containing `ok=false` and (b) MISSED
    the detector's primary case — a streak of failing shell commands, which
    `execute_shell` returns as a success-shaped `{exit_code, stdout, stderr}`
    dict carrying NO `ok` field (the tool ran fine; the *command* failed).

    The fix classifies from the structured result dict, mirroring exactly how
    the executor itself defines failure:
      - `{ok: false}` — the invariant `_annotate_error`/`_record_result`
        stamp on every error path (denied, timeout, runtime, error-dicts).
      - any dict carrying an `error` key (DL033: some tools self-report
        `{error: ...}`; classify directly, robust to field ordering).
      - a non-zero `exit_code` — a failed command in a success-shaped shell
        result the executor does not annotate.
    Policy denials (`category == "denied"`: deny-rule, user denial, hook
    block) never count — a user/policy decision is not an execution failure
    (#DL202). Successful results (`ok=True`, `exit_code=0`, no `error`)
    never match, regardless of error-related text in their *content*.
    """
    if not isinstance(result, dict):
        return False
    if result.get("category") == "denied":
        return False
    if result.get("ok") is False:
        return True
    if "error" in result:
        return True
    code = result.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def _detect_error_streak(recent_failures: list[bool]) -> int:
    """Count consecutive tool failures at the tail. A long streak means the
    agent is thrashing on something that won't start working — the four
    other detectors miss this because each retry uses a different tool or
    different args. Returning the streak length lets the caller decide.

    Takes a list of per-result failure flags (see `_result_is_failure`)
    aligned 1:1 with the recent tool results, newest last.
    """
    streak = 0
    for failed in reversed(recent_failures):
        if failed:
            streak += 1
        else:
            break
    return streak


def _detect_loop(
    call_sigs: list[str],
    recent_calls: list[str],
    recent_results: list[str],
    recent_failures: list[bool] | None = None,
) -> str | None:
    """
    Smart loop detection. Returns a reason string if loop detected, None otherwise.

    Detects:
    1. Exact repetition (same call N times)
    2. Similar calls (same tool, similar args N times)
    3. A→B→A→B cycles
    4. Stale progress (results not changing)
    5. Error streak (N consecutive tool failures — "frustration loop")

    `recent_failures` holds per-result failure flags (see `_result_is_failure`)
    aligned 1:1 with `recent_results`; when omitted the error-streak detector
    is skipped (the other four still run).
    """
    # 1. Exact repetition — Counter em vez de N x list.count() (O(N) vs O(N*M))
    counts = Counter(recent_calls)
    for sig in call_sigs:
        c = counts.get(sig, 0)
        if c >= _MAX_REPEAT_CALLS:
            return f"exact repeat: '{sig[:60]}' called {c}x"
    # #015: o loop acima só inspeciona sigs do TURNO CORRENTE. Num padrão
    # A→A→…→B (B muda a cada turno mas A se repete no histórico), A nunca está
    # em `call_sigs` e escapava o detector exato (era pego só por cycle/stale).
    # `most_common(1)` é o máximo global (Counter ordena desc). Usa o threshold
    # HISTÓRICO (2×, mais estrito) porque está desacoplado do "agora" — evita
    # cortar uma sessão produtiva só porque um sig antigo ainda está na janela.
    for sig, c in counts.most_common(1):
        if c >= _HISTORICAL_REPEAT_THRESHOLD:
            return f"exact repeat: '{sig[:60]}' called {c}x (historical)"

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
        similar_count = 0
        for s in candidates:
            if _are_similar(sig, s):
                similar_count += 1
                if similar_count >= _SIMILAR_REPEAT_CALLS:
                    break
        if similar_count >= _SIMILAR_REPEAT_CALLS:
            return f"similar calls: '{sig[:60]}' ~{similar_count}x"

    # 3. Cycle detection (A→B→A→B)
    if _detect_cycle(recent_calls):
        return "cycle detected in recent calls"

    # 4. Stale progress — passar recent_calls pra que pares com args claramente
    # distintos (ler N arquivos diferentes) nao sejam contados como stale so
    # porque os result previews compartilham um header/template comum.
    # #203: `call_sigs` é o batch corrente, já extendido em recent_calls mas
    # AINDA NÃO executado — informar o tamanho para o alinhamento call↔result.
    if _detect_stale_progress(
        recent_results, recent_calls, pending=len(call_sigs)
    ):
        return "stale progress — tool results not changing"

    # 5. Frustration loop — back-to-back tool failures
    if recent_failures:
        streak = _detect_error_streak(recent_failures)
        if streak >= _ERROR_STREAK_THRESHOLD:
            return f"error streak: {streak} consecutive failed tool calls"

    return None


