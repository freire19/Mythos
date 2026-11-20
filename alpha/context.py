"""
Context management for Mythos.

Handles intelligent message compression, token estimation, and context window
optimization. Instead of crude truncation, uses the LLM itself to summarize
old messages when context grows too large.
"""

from __future__ import annotations

import logging
import os
import re

from .attachments import extract_text
from .config import LIMITS

logger = logging.getLogger(__name__)

# ─── Token estimation ───
# Rough approximation: ~4 chars per token for English, ~3 for code-heavy content.
# Good enough for budget decisions without requiring tiktoken dependency.

CHARS_PER_TOKEN = 4
# Flat per-image budget for context accounting. Anthropic and OpenAI charge
# roughly 1k–3k tokens per typical screenshot; we use a conservative midpoint
# so the compression trigger fires before the model's real budget is hit.
IMAGE_TOKEN_COST = 1500

# DEEP_PERFORMANCE #033 + #P002: cache de estimate_messages_tokens.
# Cache key inclui fingerprint do tail porque (id, len) sozinhos nao
# detectam mutacao in-place — streaming append em messages[-1]["content"]
# ou `del msg["reasoning_content"]` preservam len.
#
# #D112: o cache vive num ContextVar (definido abaixo, junto do contador de
# falhas) em vez de globals de modulo. Globals eram compartilhados entre todos
# os agent loops concorrentes (delegate_parallel ate 3 agents); como `id()` e
# reusado apos GC, a lista de um sub-agent podia colidir com a chave-cache de
# outra lista ja liberada e retornar uma estimativa stale. ContextVar isola
# por contexto async — cada loop ve seu proprio cache.


def _tail_fingerprint(messages: list[dict]) -> tuple:
    """Cheap fingerprint do tail para invalidar o cache quando o ultimo
    message muda in-place sem alterar len(messages).

    Coleta apenas o id() e tamanhos de content/reasoning/tool_calls do
    ultimo message — strings nao sao re-iteradas. Custo O(1) amortizado.
    """
    if not messages:
        return ()
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        content_size = len(content)
    elif isinstance(content, list):
        content_size = sum(
            len(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text" else 1
            for b in content
        )
    else:
        content_size = 0
    reasoning = last.get("reasoning_content") or ""
    tool_calls = last.get("tool_calls") or ()
    return (id(last), content_size, len(reasoning), len(tool_calls))


import contextvars  # noqa: E402 — keep grouped with the failure counter

# Quantos retries de LLM-based compression antes de cair em truncacao crua.
# >= este numero de empty-summary consecutivos -> hard truncate respeitando
# tuplas assistant->tool. Reset quando uma compressao tem sucesso.
_COMPRESS_FAIL_TRUNCATE_THRESHOLD = 2

# Antes era um global int. Bug: parent + sub-agents compartilhavam o counter
# no mesmo processo. Uma falha num sub-agent consumia o budget do parent —
# atingido o threshold, *todos* caiam em hard_truncate na proxima tentativa.
# ContextVar isola por contexto async; cada agent loop ve seu proprio valor.
_compress_consecutive_failures: contextvars.ContextVar[int] = contextvars.ContextVar(
    "alpha_compress_consecutive_failures", default=0
)

# #D112: cache de estimate_messages_tokens isolado por contexto async.
# Guarda (id(messages), len, tail_fingerprint, estimate) ou None.
_token_estimate_cache: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "alpha_token_estimate_cache", default=None
)

# Fator de calibração: (tokens reais de input reportados pelo provider) /
# (estimativa por chars). O heurístico CHARS_PER_TOKEN=4 subestima código
# denso, JSON e línguas non-ASCII — e o provider já devolve a contagem exata
# no `usage` de cada turno. Realimentamos essa razão (EWMA) para que a decisão
# de compressão use a estimativa CALIBRADA, não a crua. ContextVar isola
# parent vs sub-agents concorrentes. Default 1.0 = heurístico puro até o
# primeiro turno com usage.
_token_calibration: contextvars.ContextVar[float] = contextvars.ContextVar(
    "alpha_token_calibration", default=1.0
)

# Bandas de sanidade: uma razão anômala de um turno (ex: provider ecoou 0)
# não pode destruir a estimativa. Clamp + EWMA suavizam.
_CALIBRATION_MIN = 0.5
_CALIBRATION_MAX = 3.0
_CALIBRATION_ALPHA = 0.5  # peso do turno novo no EWMA


def input_tokens_from_usage(usage: dict | None) -> int | None:
    """Total de tokens de INPUT reais de um usage dict, incluindo cache.

    Anthropic reporta `input_tokens` já EXCLUINDO tokens em cache; para o
    tamanho real do contexto somamos cache_read + cache_creation. OpenAI usa
    `prompt_tokens` e não separa cache. Retorna None quando ausente."""
    if not isinstance(usage, dict):
        return None
    base = None
    for k in ("prompt_tokens", "input_tokens"):
        v = usage.get(k)
        if v is not None:
            base = int(v)
            break
    if base is None:
        return None
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return base + cache_read + cache_creation


def record_real_token_usage(estimated_sent: int, usage: dict | None) -> None:
    """Calibra o heurístico contra os tokens reais de input do provider.

    `estimated_sent` é `estimate_messages_tokens` das mensagens efetivamente
    enviadas neste turno; `usage` traz a contagem real. Atualiza o fator de
    calibração (EWMA, clamped). No-op quando faltam dados."""
    real = input_tokens_from_usage(usage)
    if real is None or real <= 0 or estimated_sent <= 0:
        return
    ratio = real / estimated_sent
    ratio = max(_CALIBRATION_MIN, min(_CALIBRATION_MAX, ratio))
    prev = _token_calibration.get()
    _token_calibration.set(prev * (1 - _CALIBRATION_ALPHA) + ratio * _CALIBRATION_ALPHA)


def calibrated_messages_tokens(messages: list[dict]) -> int:
    """`estimate_messages_tokens` ajustado pelo fator de calibração aprendido
    com o `usage` real. É a estimativa que as decisões de budget devem usar."""
    return int(estimate_messages_tokens(messages) * _token_calibration.get())

# #DM075: budget único para modelos de 1M de contexto (reserva ~100K para
# response + tool schemas) — usado por Gemini e pelas detecções de modelos
# 1M em get_context_limit (Anthropic "[1m]", DeepSeek v4/v5).
_ONE_MEGA_CONTEXT_BUDGET = 900_000

# Context window sizes per provider (conservative estimates leaving room for response)
PROVIDER_CONTEXT_LIMITS: dict[str, int] = {
    "deepseek": 60_000,   # 64K context, reserve 4K for response
    "openai": 120_000,    # 128K context, reserve 8K for response
    "anthropic": 180_000, # 200K context, reserve 20K for response
    "grok": 120_000,      # 128K context, reserve 8K
    "google": _ONE_MEGA_CONTEXT_BUDGET,  # Gemini 2.5/3.x 1M context, reserve 100K
    "ollama": 28_000,     # varies, conservative default
}

# Compression triggers when usage exceeds this fraction of the context window
COMPRESSION_THRESHOLD = 0.70

# Keep the last N messages untouched (system + recent exchanges)
PROTECTED_TAIL_MESSAGES = 8

# Minimum messages before compression is even considered
MIN_MESSAGES_FOR_COMPRESSION = 12

# Cap absoluto de messages em memoria/payload — independente da estimativa
# de tokens. Tool results pequenos (< 200 chars) podem nao gatilhar o
# threshold por tokens mesmo com milhares de messages, mas cada iteracao
# reserializa toda a lista para JSON e o `_detect_loop` itera N entradas.
# #DM073: o valor canonico vive em config.LIMITS["max_messages"] — antes era
# uma copia morta la e o cap real duplicado aqui.
MAX_MESSAGES = LIMITS["max_messages"]


def estimate_tokens(text: str) -> int:
    """Estimate token count from text length."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def _estimate_content_tokens(content) -> int:
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                total += estimate_tokens(block.get("text", ""))
            elif btype in ("image_url", "image"):
                total += IMAGE_TOKEN_COST
        return total
    return 0


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across all messages.

    DEEP_PERFORMANCE #033 + #P002: cache por (id, len, tail_fingerprint).
    Chamada 4x por turno (needs_compression, _context_pct,
    compress_until_under_budget, logging). O fingerprint do tail captura
    mutacoes in-place do ultimo message — append de streaming, `del
    reasoning_content`, etc. — que (id, len) sozinhos perdiam.
    """
    mid = id(messages)
    mlen = len(messages)
    fp = _tail_fingerprint(messages)
    cached = _token_estimate_cache.get()
    if cached is not None:
        c_mid, c_mlen, c_fp, c_est = cached
        if mid == c_mid and mlen == c_mlen and fp == c_fp:
            return c_est

    total = 0
    for msg in messages:
        total += _estimate_content_tokens(msg.get("content"))
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                total += estimate_tokens(fn.get("name", ""))
                total += estimate_tokens(fn.get("arguments", ""))
        reasoning = msg.get("reasoning_content")
        if reasoning:
            total += estimate_tokens(reasoning)

    _token_estimate_cache.set((mid, mlen, fp, total))
    return total


def get_context_limit(provider: str) -> int:
    """Get the token budget for a provider.

    Honors `ALPHA_CONTEXT_LIMIT` as a global override. For Anthropic, also
    detects 1M extended-context models from the model name (e.g. an env
    `ANTHROPIC_MODEL` containing "1m" or "[1m]") and bumps the budget.
    """
    override = os.environ.get("ALPHA_CONTEXT_LIMIT", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            pass

    from .config import base_provider
    provider = base_provider(provider)
    if provider == "anthropic":
        model = os.environ.get("ANTHROPIC_MODEL", "").lower()
        if "1m" in model:
            # 1M context — leave ~100K headroom for response + tool schemas.
            return _ONE_MEGA_CONTEXT_BUDGET

    if provider == "deepseek":
        # DeepSeek V4 (Pro/Flash) ships with 1M context; legacy v3 chat/coder
        # remain at 64K and fall through to PROVIDER_CONTEXT_LIMITS below.
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").lower()
        if "v4" in model or "v5" in model:
            return _ONE_MEGA_CONTEXT_BUDGET

    return PROVIDER_CONTEXT_LIMITS.get(provider, 28_000)


_CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "context length",
    "prompt is too long",
    "request_too_large",
    "exceeds the maximum",
    "exceed the maximum",
    "too many tokens",
    "input is too long",
)

# DEEP_PERFORMANCE #046: single compiled, case-insensitive scan instead of
# allocating a full lowercase copy of error_msg and probing it N times.
_CONTEXT_OVERFLOW_RE = re.compile(
    "|".join(re.escape(m) for m in _CONTEXT_OVERFLOW_MARKERS), re.IGNORECASE
)


def is_context_overflow_error(error_msg: str | None) -> bool:
    """Detect whether an LLM error string indicates a context-window overflow."""
    if not error_msg:
        return False
    return _CONTEXT_OVERFLOW_RE.search(error_msg) is not None


def needs_compression(messages: list[dict], provider: str) -> bool:
    """Check if messages exceed the compression threshold."""
    if len(messages) < MIN_MESSAGES_FOR_COMPRESSION:
        return False
    # Hard cap por contagem: muitas messages pequenas nao disparam o cap
    # por tokens mas degradam payload SSE e loop detection.
    if len(messages) > MAX_MESSAGES:
        return True
    limit = get_context_limit(provider)
    # Estimativa CALIBRADA pelo usage real do provider (ver
    # record_real_token_usage) — evita overflow por subestimativa em código
    # denso, que a heurística chars/4 sozinha não pega.
    current = calibrated_messages_tokens(messages)
    return current > (limit * COMPRESSION_THRESHOLD)


def _find_compressible_range(
    messages: list[dict],
    tail: int = PROTECTED_TAIL_MESSAGES,
) -> tuple[int, int]:
    """
    Find the range of messages that can be compressed.

    Returns (start, end) indices. Protects:
    - Index 0: system prompt
    - Last `tail` messages (default PROTECTED_TAIL_MESSAGES)

    Importante: a tail protegida nunca pode comecar com `role=tool` orfa
    (assistant.tool_calls correspondente caiu dentro do range comprimido)
    nem o range pode terminar entre `assistant.tool_calls` e suas `tool`
    responses — ambos os casos sao rejeitados pela API com HTTP 400 apos
    a compressao executar.
    """
    if len(messages) <= tail + 1:
        return (0, 0)  # nothing to compress

    start = 1  # skip system prompt
    end = len(messages) - tail
    if end <= start:
        return (0, 0)

    # Recuar 'end' enquanto a primeira mensagem do tail for `role=tool`
    # (orfa, sem o assistant.tool_calls correspondente acima).
    # AUDIT_V1.10 #001: guard contra underflow — `end < len` permitia
    # indices negativos em batch de 8+ tools sem assistant intermediario.
    while end > start and messages[end].get("role") == "tool":
        end -= 1
    # Recuar tambem se o range comprimido terminaria com assistant.tool_calls
    # (cortando antes das tool responses dele).
    while end > start and (
        messages[end - 1].get("role") == "assistant"
        and messages[end - 1].get("tool_calls")
    ):
        end -= 1
    if end <= start:
        return (0, 0)
    return (start, end)


# #DM072: alias público — o agent loop usa isto no fallback de overflow; o
# nome underscore fica como retrocompat interna.
find_compressible_range = _find_compressible_range


def build_compression_prompt(
    messages: list[dict], start: int, end: int, instructions: str = "",
) -> str:
    """
    Build a prompt asking the LLM to summarize a range of messages.

    `instructions` (from `/compact <foco>`) tells the summarizer what the
    user wants kept in detail; everything else is still summarized.

    Returns a user message containing the conversation segment to compress.
    """
    lines = []
    for msg in messages[start:end]:
        role = msg.get("role", "unknown")
        raw_content = msg.get("content")
        content = extract_text(raw_content) if raw_content else ""
        # Note in the summary that the original message had image attachments,
        # otherwise the model has no idea they ever existed.
        image_count = (
            sum(1 for b in raw_content if isinstance(b, dict)
                and b.get("type") in ("image_url", "image"))
            if isinstance(raw_content, list) else 0
        )
        image_note = f" [+{image_count} image(s)]" if image_count else ""

        if role == "assistant" and msg.get("tool_calls"):
            tc_names = [tc.get("function", {}).get("name", "unknown")
                        for tc in msg["tool_calls"]]
            lines.append(f"[assistant called tools: {', '.join(tc_names)}]")
            if content:
                lines.append(f"[assistant text]: {content[:500]}")
        elif role == "tool":
            preview = content[:800] if content else "(empty)"
            tool_id = msg.get("tool_call_id", "")
            lines.append(f"[tool result {tool_id}]: {preview}")
        else:
            preview = content[:1000] if content else "(empty)"
            lines.append(f"[{role}{image_note}]: {preview}")

    conversation_text = "\n".join(lines)

    focus = ""
    if instructions and instructions.strip():
        from ._text_safety import strip_control_and_bidi
        focus = (
            "The user asked the summary to focus on: "
            f"{strip_control_and_bidi(instructions.strip())[:500]}. Keep every detail "
            "relevant to that focus verbatim where possible; summarize the rest briefly.\n\n"
        )
    return (
        "Summarize the following conversation segment into a concise but complete "
        "summary. Preserve ALL important information: file paths, code snippets, "
        "tool results, decisions made, errors encountered, and key findings. "
        "Format as bullet points. Be thorough but concise.\n\n"
        f"{focus}"
        f"--- CONVERSATION SEGMENT ---\n{conversation_text}\n--- END SEGMENT ---"
    )


def _strip_old_reasoning(messages: list[dict]) -> None:
    """Keep reasoning_content only on the most recent assistant message.

    AUDIT_V1.8 #049: logica duplicada em _hard_truncate e compress_context.
    Helper unico evita drift entre os dois sites.
    """
    last_assistant_seen = False
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            if not last_assistant_seen:
                last_assistant_seen = True
            elif "reasoning_content" in messages[i]:
                del messages[i]["reasoning_content"]


def _hard_truncate(
    messages: list[dict], start: int, end: int, keep_recent: int = 8
) -> list[dict]:
    """Drop a range of old messages without an LLM summary.

    Fallback usado quando o LLM nao retorna sumario (provider outage, empty
    response). Mantem `messages[0]` (system prompt) + os ultimos
    `keep_recent` messages, removendo qualquer mensagem `role=tool` que
    sobrar sem o assistant.tool_calls correspondente — caso contrario o
    proximo request quebra com HTTP 400.

    Tambem remove `assistant.tool_calls` do final da tail quando nao ha
    tool responses correspondentes (DL022: tail `[user, assistant.tc]`
    causava HTTP 400 porque tool_calls sem responses quebram a API).
    """
    # AUDIT_V1.8 #R203: guard contra messages=[] precisa proteger AMBOS
    # `sys_msg = messages[0]` (linha 333 antiga, crashava com IndexError) E
    # `tail_source = messages[1:]`. A versao anterior so guardava a segunda,
    # criando guard inconsistente que mascarava o crash da primeira.
    if not messages:
        return []
    sys_msg = messages[0]
    # AUDIT_V1.8 #019: `messages[-keep_recent:]` em listas com `len <= keep_recent`
    # retorna a lista inteira, incluindo `messages[0]` (system). Apos
    # `result = [sys_msg] + tail`, o system aparecia duplicado — em Anthropic
    # (que concatena system parts com \n\n no adapter) viravam instrucoes
    # dobradas e respostas erraticas (saudacoes repetidas, regras conflitantes).
    # Slice a partir de `messages[1:]` exclui o system explicitamente.
    tail_source = messages[1:]
    tail = list(tail_source[-keep_recent:]) if keep_recent > 0 else []
    # 1. Drop tool messages orfas do INICIO da tail.
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)
    # 2. Drop assistant.tool_calls do FINAL sem tool responses.
    while tail and tail[-1].get("role") == "assistant" and tail[-1].get("tool_calls"):
        tcs = tail[-1]["tool_calls"]
        tc_ids = {tc.get("id") for tc in tcs if tc.get("id")}
        has_response = any(
            m.get("role") == "tool" and m.get("tool_call_id") in tc_ids
            for m in tail[:-1]
        )
        if has_response:
            break
        tail.pop()
    # 3. Drop reasoning_content from all but the last assistant in the tail
    #    (DL030: hard_truncate nao limpava reasoning antigo, acumulando bloat
    #    de thinking tokens do DeepSeek-reasoner no fallback path).
    result = [sys_msg] + tail
    _strip_old_reasoning(result)
    return result


# #DM072: alias público — o agent loop usa isto no fallback de overflow; o
# nome underscore fica como retrocompat interna.
hard_truncate = _hard_truncate


async def compress_context(
    messages: list[dict],
    provider: str,
    stream_fn,
    protected_tail: int = PROTECTED_TAIL_MESSAGES,
    instructions: str = "",
) -> list[dict]:
    """
    Compress old messages by summarizing them via the LLM.

    Args:
        messages: Full message list (mutated in-place).
        provider: LLM provider for the compression call.
        stream_fn: The stream_chat_with_tools function for making LLM calls.
        protected_tail: Number of trailing messages to leave untouched. Lower
            this to free more tokens when the recent tail itself is huge.
        instructions: optional focus for the summary (`/compact <foco>`).

    Returns:
        The compressed messages list.
    """
    start, end = _find_compressible_range(messages, protected_tail)
    if start >= end:
        logger.debug("Nothing to compress — range too small")
        return messages

    tokens_before = estimate_messages_tokens(messages)

    # Build compression request
    compression_prompt = build_compression_prompt(messages, start, end, instructions)

    compression_messages = [
        {
            "role": "system",
            "content": (
                "You are a conversation summarizer. Produce a concise bullet-point "
                "summary preserving all key technical details: file paths, function "
                "names, error messages, decisions, and outcomes. Do not add opinions."
            ),
        },
        {"role": "user", "content": compression_prompt},
    ]

    # Call LLM without tools for summarization — routed through
    # ALPHA_COMPRESS_PROVIDER when set (a cheaper model summarizes just as
    # well; the main loop keeps the strong one).
    from .config import route_provider
    compress_provider = route_provider("compress", provider)
    if compress_provider != provider:
        logger.info("context compression routed to %s", compress_provider)
    summary = ""
    try:
        async for event in stream_fn(compression_messages, [], 0.2, provider=compress_provider):
            if event["type"] == "content_token":
                summary += event["token"]
            elif event["type"] == "final":
                if event.get("content"):
                    summary = event["content"]
    except Exception:
        # logger.exception preserves exc_info; logger.warning would drop the
        # traceback we need to diagnose real failures here.
        logger.exception("Compression LLM call failed")
        summary = ""

    if not summary:
        failures = _compress_consecutive_failures.get() + 1
        _compress_consecutive_failures.set(failures)
        if failures >= _COMPRESS_FAIL_TRUNCATE_THRESHOLD:
            logger.warning(
                f"Compression failed {failures}x "
                "consecutive — falling back to hard truncation"
            )
            new_messages = _hard_truncate(messages, start, end)
            tokens_after = estimate_messages_tokens(new_messages)
            logger.info(
                f"Hard-truncated context: {tokens_before} -> {tokens_after} "
                f"tokens ({len(messages) - len(new_messages)} messages dropped)"
            )
            messages[:] = new_messages
            # AUDIT_V1.8 #018: sem reset apos hard-truncate, o contador fica
            # >= threshold para sempre. Toda compressao subsequente cai aqui
            # mesmo apos o LLM voltar a funcionar — silently degrada a sessao
            # ate o final. Reset garante que apenas uma janela transitoria de
            # falha dispara hard-truncate; o proximo summary com sucesso volta
            # ao path normal.
            _compress_consecutive_failures.set(0)
            return messages
        logger.warning(
            f"Compression produced empty summary — skipping "
            f"(failure {failures}/{_COMPRESS_FAIL_TRUNCATE_THRESHOLD})"
        )
        return messages

    # Reset failure counter on successful summary
    _compress_consecutive_failures.set(0)

    # Replace compressed messages with a single summary message
    summary_message = {
        "role": "user",
        "content": (
            f"[CONTEXT SUMMARY — compressed from {end - start} messages]\n\n"
            f"{summary}\n\n"
            "[End of summary. The conversation continues below.]"
        ),
    }

    # Rebuild: system + summary + protected tail
    new_messages = (
        [messages[0]]          # system prompt
        + [summary_message]    # compressed summary
        + messages[end:]       # recent messages (protected tail)
    )

    # AUDIT_V1.2 #022: drop reasoning_content from all but the last
    # assistant message — the DeepSeek API requires it ONLY for the
    # immediately preceding assistant turn. Carrying it forward across
    # compressed messages bloats context and risks stale thinking leaks.
    _strip_old_reasoning(new_messages)

    tokens_after = estimate_messages_tokens(new_messages)
    logger.info(
        f"Context compressed: {tokens_before} -> {tokens_after} tokens "
        f"({end - start} messages summarized)"
    )

    # Mutate in-place so the caller's reference stays valid
    messages[:] = new_messages
    # DEEP_PERFORMANCE #044: prime the token cache for the post-mutation
    # `messages` identity. `tokens_after` above was keyed on `new_messages`
    # (a distinct id); without this, the caller's next measure
    # (compress_until_under_budget L563, and the next pass's internal measure)
    # would miss the cache and walk every message again. Content is identical
    # after the slice-assign and `messages[-1] is new_messages[-1]`, so the id,
    # len, and tail fingerprint all match — the cached estimate stays valid.
    _token_estimate_cache.set(
        (id(messages), len(messages), _tail_fingerprint(messages), tokens_after)
    )
    return messages


async def compress_until_under_budget(
    messages: list[dict],
    provider: str,
    stream_fn,
    target_tokens: int | None = None,
    max_passes: int = 3,
) -> tuple[int, int]:
    """Compress repeatedly, shrinking the protected tail each pass, until the
    estimated token count drops to `target_tokens` (defaults to 60% of the
    provider's context limit) or `max_passes` runs out. Also runs when
    `len(messages)` exceeds `MAX_MESSAGES`, eroding the tail until the
    count drops under the cap even with tokens already under budget.

    Returns (tokens_before, tokens_after). Mutates `messages` in place.

    Each pass uses a smaller protected tail so that, when the bulk of tokens
    sits in recent tool results (e.g. huge file dumps), we still claw budget
    back instead of looping at -1% reductions.
    """
    tokens_before = estimate_messages_tokens(messages)

    if target_tokens is None:
        target_tokens = int(get_context_limit(provider) * 0.6)

    if tokens_before <= target_tokens and len(messages) <= MAX_MESSAGES:
        return tokens_before, tokens_before

    # Adaptive tail schedule — first pass keeps the standard buffer; later
    # passes erode it down to a single message if the tail itself is the bloat.
    tail_schedule = [
        PROTECTED_TAIL_MESSAGES,
        max(2, PROTECTED_TAIL_MESSAGES // 2),
        1,
    ]
    passes = min(max_passes, len(tail_schedule))

    current = tokens_before
    for i in range(passes):
        tail = tail_schedule[i]
        len_before_pass = len(messages)
        try:
            await compress_context(
                messages, provider, stream_fn, protected_tail=tail
            )
        except Exception as e:
            logger.warning(f"Compression pass {i + 1} failed: {e}")
            break

        new_total = estimate_messages_tokens(messages)
        logger.info(
            f"Compression pass {i + 1}/{passes} (tail={tail}): "
            f"{current} -> {new_total} tokens"
        )
        # Stop if the pass made no meaningful progress (avoid infinite churn).
        if new_total >= current * 0.98 and len(messages) >= len_before_pass:
            current = new_total
            break
        current = new_total
        if current <= target_tokens and len(messages) <= MAX_MESSAGES:
            break

    return tokens_before, current
