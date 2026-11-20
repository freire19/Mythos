"""Anthropic Messages API adapter.

Translates between the OpenAI chat-completions shape used by the rest of
the agent and Anthropic's `/v1/messages` endpoint:

- Header: `x-api-key` instead of `Authorization: Bearer`
- System message: separate top-level `system` field, not a message
- Tool result: `{"role": "user", "content": [{"type": "tool_result", ...}]}`
- Tool definition: `input_schema` instead of `parameters`
- Streaming: `content_block_delta` events with `text_delta` or `input_json_delta`

The streaming generator yields the same `content_token` / `final` events
the rest of the loop already consumes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator

import httpx

from ._http_singleton import LoopAwareClient
from ._rate_limiter import acquire_llm_token as _rate_limit_acquire

from ._security_log import sanitize_for_log
from .config import HTTPX_LIMITS_LLM, LLM_TIMEOUT, RETRY, env_bool
# #DM072: consome os nomes públicos de llm — os aliases underscore antigos
# seguem existindo lá apenas como retrocompat interna.
from .llm import (
    DsmlStripper,
    StreamAccumulator,
    calc_backoff,
    drain_dsml_tail,
    parse_retry_after,
)

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
# Default output cap. Configurable via ALPHA_MAX_TOKENS so long generations
# (big refactors, full-file rewrites) aren't silently truncated at 8192. The
# 8192 default is Anthropic's safe universal cap; newer models allow far more.
_DEFAULT_MAX_TOKENS = 8192

# Ephemeral prompt-cache breakpoint. Applied to the large, stable prefixes
# re-sent every agentic turn (system prompt + tool schemas + conversation
# prefix) so Anthropic bills them at ~0.1x on cache hits and skips re-encoding.
# Kill-switch: ALPHA_ANTHROPIC_CACHE=0.
_CACHE_CONTROL = {"type": "ephemeral"}


def _max_tokens() -> int:
    """Resolve the output token cap from ALPHA_MAX_TOKENS, clamped to a sane
    floor. Invalid / non-positive values fall back to the default."""
    raw = os.environ.get("ALPHA_MAX_TOKENS", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            logger.warning("Invalid ALPHA_MAX_TOKENS=%r — using default %d", raw, _DEFAULT_MAX_TOKENS)
    return _DEFAULT_MAX_TOKENS


def _prompt_cache_enabled() -> bool:
    """Whether to attach ephemeral cache_control breakpoints. On by default;
    set ALPHA_ANTHROPIC_CACHE=0 to disable (e.g. a proxy that rejects the
    cache_control field)."""
    return env_bool("ALPHA_ANTHROPIC_CACHE", default=True)


# ── Extended thinking ──
#
# Sem `thinking` no payload, modelos Claude 3.7+ rodam sem o raciocínio
# estendido — degrada silenciosamente a qualidade em tarefas agênticas.
# Tri-state via ALPHA_ANTHROPIC_THINKING: auto (default, liga para modelos
# que suportam), on (força), off. Budget via ALPHA_THINKING_BUDGET.
# Os thinking blocks retornados precisam voltar VERBATIM (com signature)
# na próxima request quando o turno tem tool_use — ver _convert_messages.

_THINKING_DEFAULT_BUDGET = 4096
_THINKING_MIN_BUDGET = 1024  # mínimo aceito pela API

# claude-3-7-* e famílias com major >= 4 (opus/sonnet/haiku/fable/mythos).
_THINKING_MODEL_RE = re.compile(
    r"claude-(?:3-7|(?:opus|sonnet|haiku|fable|mythos)-[4-9])"
)


def _thinking_setting() -> str:
    raw = os.environ.get("ALPHA_ANTHROPIC_THINKING", "auto").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return "off"
    if raw in ("1", "true", "yes", "on"):
        return "on"
    return "auto"


def _model_supports_thinking(model: str) -> bool:
    return bool(_THINKING_MODEL_RE.search(model.lower()))


def _thinking_enabled(model: str) -> bool:
    setting = _thinking_setting()
    if setting == "off":
        return False
    if setting == "on":
        return True
    return _model_supports_thinking(model)


def _thinking_budget() -> int:
    raw = os.environ.get("ALPHA_THINKING_BUDGET", "").strip()
    if raw:
        try:
            return max(_THINKING_MIN_BUDGET, int(raw))
        except ValueError:
            logger.warning(
                "Invalid ALPHA_THINKING_BUDGET=%r — using default %d",
                raw, _THINKING_DEFAULT_BUDGET,
            )
    return _THINKING_DEFAULT_BUDGET
_TRANSIENT_HTTPX_ERRORS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    # AUDIT_V2.3 #305: reset/queda da conexão DURANTE leitura/escrita (não
    # timeout) também é transitório — cair no `except Exception` genérico
    # encerrava sem retry mesmo antes do 1º token, quando o replay é seguro.
    httpx.ReadError,
    httpx.WriteError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

# Loop-aware shared client; #DM042 migrou para LoopAwareClient
# (alpha/_http_singleton.py) — mesmo padrao usado por llm.py e web_search.py.
# `timeout` deixou de ser parametro: stream_anthropic so passa LLM_TIMEOUT
# (visto no unico call site em _stream_anthropic_provider). Per-request
# timeout override iria via client.request(timeout=...) — nao usado aqui.
_client = LoopAwareClient(
    name="llm_anthropic",
    build=lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(LLM_TIMEOUT, connect=10.0),
        limits=httpx.Limits(**HTTPX_LIMITS_LLM),
    ),
)


async def _get_client() -> httpx.AsyncClient:
    """Return the loop-aware shared httpx.AsyncClient for Anthropic.

    AUDIT_V2.3 #502: o antigo parâmetro `timeout` era declarado e ignorado
    (assinatura mentia sobre a capacidade) — removido. O timeout do client
    vem de LLM_TIMEOUT no build; override por-request deve ir via
    `client.request(timeout=)`."""
    return await _client.get()


# ── OpenAI → Anthropic conversion ──

# AUDIT_V2.3 #400: a lista `tools` (OpenAI-format) é construída UMA vez por
# sessão e nunca muda entre iterações, mas era reconvertida (~70 rebuilds de
# dict) a cada chamada LLM. Cache por IDENTIDADE com referência FORTE à
# lista de origem — segurar a referência impede GC/reuso de id(), tornando
# a comparação `is` sound (nota da review: chave `(id, len)` sem ref podia
# devolver o schema errado após GC). Slot único: sub-agentes com listas
# filtradas próprias apenas anulam o ganho (reconversão), sem risco de
# correção. O dict cacheado NUNCA é mutado — o breakpoint de cache_control
# é aplicado sobre cópia rasa no _build_anthropic_request.
_TOOLS_CACHE: tuple[list, list] | None = None


def _convert_tools(openai_tools: list[dict]) -> list[dict]:
    """Convert OpenAI function-tool schema to Anthropic tool schema."""
    global _TOOLS_CACHE
    if not openai_tools:
        return []
    cached = _TOOLS_CACHE
    if cached is not None and cached[0] is openai_tools:
        return cached[1]
    out = []
    for t in openai_tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        out.append(
            {
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    _TOOLS_CACHE = (openai_tools, out)
    return out


_DATA_URL_PREFIX = "data:"


def _convert_user_content(content) -> str | list[dict]:
    """Convert OpenAI user content (str or block list) to Anthropic shape.

    OpenAI image_url blocks become Anthropic image blocks (base64 source).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)

    blocks: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": b.get("text", "")})
        elif btype == "image_url":
            # AUDIT_V2.3 #402: o partition da data-URL materializa uma cópia
            # da string base64 (50-200KB) POR screenshot POR chamada LLM.
            # As mensagens de visão são imutáveis após criadas — cacheia o
            # bloco `source` convertido dentro do próprio bloco OpenAI. A
            # chave "_"-prefixada segue a convenção existente (o executor
            # descarta "_"-keys ao serializar) e não vaza no wire OpenAI.
            cached_src = b.get("_anthropic_source")
            if cached_src is not None:
                blocks.append({"type": "image", "source": cached_src})
                continue
            url = (b.get("image_url") or {}).get("url", "")
            if url.startswith(_DATA_URL_PREFIX):
                # data:<media_type>;base64,<data>
                head, _, data = url.partition(",")
                media_type = head[len(_DATA_URL_PREFIX):].split(";", 1)[0] or "image/png"
                src = {"type": "base64", "media_type": media_type, "data": data}
                b["_anthropic_source"] = src
                blocks.append({"type": "image", "source": src})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks if blocks else ""


def _as_blocks(content) -> list[dict]:
    """Promote a user content value (str or block list) to a block list."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        return content
    return []


def _convert_messages(
    openai_messages: list[dict], include_thinking: bool = False
) -> tuple[str, list[dict]]:
    """Split system messages out and convert the rest to Anthropic shape.

    `include_thinking`: replay `thinking_blocks` (armazenados verbatim na
    mensagem assistant pelo executor) como blocos iniciais do turno — a API
    exige o thinking original (com signature) antes de tool_use quando o
    extended thinking está ativo, e rejeita thinking blocks quando inativo.

    Adjacent tool-result messages are coalesced into a single `user` turn
    with multiple `tool_result` content blocks (Anthropic requires this).
    Consecutive `user` turns are merged into one: the executor appends a
    vision user message (tool screenshot) right after the tool messages of
    a batch, and Anthropic rejects non-alternating roles. The merge keeps
    tool_result blocks first in the turn, as the API requires.
    """
    system_parts: list[str] = []
    converted: list[dict] = []
    pending_tool_results: list[dict] = []

    def append_user(content) -> None:
        if converted and converted[-1].get("role") == "user":
            merged = _as_blocks(converted[-1].get("content")) + _as_blocks(content)
            converted[-1]["content"] = merged if merged else ""
            return
        converted.append({"role": "user", "content": content})

    def flush_tool_results():
        if pending_tool_results:
            append_user(list(pending_tool_results))
            pending_tool_results.clear()

    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            continue

        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                }
            )
            continue

        flush_tool_results()

        if role == "user":
            append_user(_convert_user_content(content))
            continue

        if role == "assistant":
            blocks: list[dict] = []
            if include_thinking:
                for tb in msg.get("thinking_blocks") or []:
                    if isinstance(tb, dict) and tb.get("type") in (
                        "thinking", "redacted_thinking",
                    ):
                        blocks.append(tb)
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    # #D119: a prior turn produced malformed args (e.g. DSML
                    # recovered verbatim). Dropping them to {} silently means
                    # a tool gets replayed with empty input on the next
                    # Anthropic turn — log so corrupted round-trips are visible.
                    logger.warning(
                        "Anthropic replay: tool '%s' has malformed JSON args, dropping to {}",
                        fn.get("name", ""),
                    )
                    args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            converted.append({"role": "assistant", "content": blocks})
            continue

    flush_tool_results()

    return "\n\n".join(system_parts), converted


# ── Streaming ──


class _AnthropicStreamState:
    """Per-attempt accumulators for one Anthropic streaming call.

    Mirrors `_StreamState` in `llm.py`: held outside `stream_anthropic`
    so the event applier is a pure function of state. `blocks` maps
    Anthropic's `index` → `{type, text|input_json, id?, name?}`.
    `stopped` is set when the SSE loop sees `message_stop` so the caller
    can break the line loop with a single flag instead of branching on
    a dual return value.
    """

    __slots__ = (
        "accumulated_content",
        "blocks",
        "dsml_stripper",
        "last_usage",
        "yielded_any",
        "stopped",
        "pending_thinking",
    )

    def __init__(self) -> None:
        self.accumulated_content = StreamAccumulator()
        self.blocks: dict[int, dict] = {}
        self.dsml_stripper = DsmlStripper()
        # Thinking deltas received since the last drain. The SSE loop pops
        # them after each event and yields `thinking_token` so the UI can
        # render the reasoning live. Kept as a side-channel (not the return
        # value of `_apply_anthropic_event`) so the content-token contract
        # stays `str | None`.
        self.pending_thinking: list[str] = []
        # Anthropic emits input_tokens in `message_start` and final
        # output_tokens in `message_delta`. Both stay in the same dict
        # shape so alpha.cost.record_usage handles both providers
        # without per-provider branches.
        self.last_usage: dict = {}
        self.yielded_any = False
        self.stopped = False


def _mark_last_message_cacheable(anthropic_messages: list[dict]) -> None:
    """Attach a cache breakpoint to the tail of the conversation prefix.

    In an agentic loop each turn's messages are the previous turn's plus new
    ones, so a breakpoint at the current last message becomes a cacheable
    prefix on the next turn (incremental caching). Content that is a bare
    string is promoted to a single text block so cache_control has somewhere
    to live; a block list gets the marker on its last block.
    """
    if not anthropic_messages:
        return
    last = anthropic_messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return
        last["content"] = [
            {"type": "text", "text": content, "cache_control": dict(_CACHE_CONTROL)}
        ]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = dict(_CACHE_CONTROL)


def _build_anthropic_request(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    model: str,
    api_key: str,
) -> tuple[dict, dict]:
    """Build (headers, payload) for the Anthropic /v1/messages POST."""
    thinking = _thinking_enabled(model)
    system_text, anthropic_messages = _convert_messages(
        messages, include_thinking=thinking
    )
    anthropic_tools = _convert_tools(tools)
    cache = _prompt_cache_enabled()

    payload: dict = {
        "model": model,
        "max_tokens": _max_tokens(),
        "messages": anthropic_messages,
        "stream": True,
        "temperature": temperature,
    }
    if thinking:
        budget = _thinking_budget()
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Budget é adicional ao cap de saída visível (ALPHA_MAX_TOKENS
        # continua sendo o teto do texto final); max_tokens > budget é
        # exigência da API. Temperature != 1 é rejeitada com thinking.
        payload["max_tokens"] = _max_tokens() + budget
        payload["temperature"] = 1
    if system_text:
        if cache:
            # Structured system block so the stable ~20KB system prompt is
            # cached across turns (billed ~0.1x on hits).
            payload["system"] = [
                {"type": "text", "text": system_text, "cache_control": dict(_CACHE_CONTROL)}
            ]
        else:
            payload["system"] = system_text
    if anthropic_tools:
        if cache:
            # Breakpoint on the last tool caches the whole tool-schema block.
            # #400: cópia rasa da lista + do último dict — o schema cacheado
            # em _TOOLS_CACHE nunca é mutado.
            anthropic_tools = [
                *anthropic_tools[:-1],
                {**anthropic_tools[-1], "cache_control": dict(_CACHE_CONTROL)},
            ]
        payload["tools"] = anthropic_tools
    if cache:
        _mark_last_message_cacheable(anthropic_messages)

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    return headers, payload


def _apply_anthropic_event(event: dict, state: _AnthropicStreamState) -> str | None:
    """Apply one streaming Anthropic event to ``state``. Returns any
    user-visible text to yield as a ``content_token``, or None. Sets
    ``state.stopped = True`` on ``message_stop``."""
    etype = event.get("type")

    if etype == "message_start":
        msg_usage = (event.get("message") or {}).get("usage") or {}
        if msg_usage:
            state.last_usage.update(msg_usage)
    elif etype == "message_delta":
        delta_usage = event.get("usage") or {}
        if delta_usage:
            state.last_usage.update(delta_usage)

    if etype == "content_block_start":
        # #D102: index/content_block missing must not raise — a malformed
        # event would otherwise hit the generic `except` in stream_anthropic,
        # be treated as non-transient, and kill the turn with no retry.
        idx = event.get("index")
        block = event.get("content_block") or {}
        if idx is None:
            return None
        btype = block.get("type")
        if btype == "text":
            state.blocks[idx] = {"type": "text", "text": ""}
        elif btype == "tool_use":
            state.blocks[idx] = {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input_json": [],
            }
        elif btype == "thinking":
            state.blocks[idx] = {"type": "thinking", "thinking": [], "signature": []}
        elif btype == "redacted_thinking":
            state.blocks[idx] = {
                "type": "redacted_thinking",
                "data": block.get("data", ""),
            }

    elif etype == "content_block_delta":
        idx = event.get("index")
        if idx is None:
            return None
        delta = event.get("delta", {})
        block = state.blocks.get(idx)
        if block is None:
            # #D103: a delta arrived for an index with no recorded
            # content_block_start (protocol anomaly) or for a block type we
            # don't store. Log at debug so a lost tool_use is at least
            # diagnosable without drowning normal runs.
            logger.debug("Anthropic delta for unknown block index %s (%s)",
                         idx, delta.get("type"))
            return None
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                safe = state.dsml_stripper.feed(text)
                if safe:
                    return safe
        elif delta.get("type") == "input_json_delta":
            block.setdefault("input_json", []).append(delta.get("partial_json", ""))
        elif delta.get("type") == "thinking_delta":
            piece = delta.get("thinking", "")
            block.setdefault("thinking", []).append(piece)
            if piece:
                state.pending_thinking.append(piece)
        elif delta.get("type") == "signature_delta":
            block.setdefault("signature", []).append(delta.get("signature", ""))

    elif etype == "message_stop":
        state.stopped = True

    return None


def _collect_tool_calls(blocks: dict[int, dict]) -> list[dict]:
    """Pick tool_use blocks into the final tool_calls list.

    Anthropic emits `content_block_start` events with monotonically
    increasing indices and CPython 3.7+ preserves dict insertion order,
    so iterating `blocks.values()` produces them in original order.

    Logs a warning if any input_json is malformed but keeps the raw
    string so callers see what arrived.
    """
    tool_calls = []
    for block in blocks.values():
        if block["type"] != "tool_use":
            continue
        args_json = "".join(block.get("input_json") or ()) or "{}"
        try:
            json.loads(args_json)
        except json.JSONDecodeError:
            logger.warning(
                "Anthropic tool '%s' returned malformed JSON args (kept verbatim)",
                block.get("name"),
            )
        tool_calls.append({
            "id": block.get("id", ""),
            "name": block.get("name", ""),
            "arguments": args_json,
        })
    return tool_calls


def _collect_thinking_blocks(blocks: dict[int, dict]) -> list[dict]:
    """Pick thinking/redacted_thinking blocks (in order) for round-trip.

    A API exige os blocos VERBATIM (incluindo `signature`) antes do tool_use
    quando o turno seguinte devolve tool_results com thinking ativo.
    """
    out: list[dict] = []
    for block in blocks.values():
        if block.get("type") == "thinking":
            thinking = "".join(block.get("thinking") or ())
            if thinking:
                out.append({
                    "type": "thinking",
                    "thinking": thinking,
                    "signature": "".join(block.get("signature") or ()),
                })
        elif block.get("type") == "redacted_thinking" and block.get("data"):
            out.append({"type": "redacted_thinking", "data": block.get("data", "")})
    return out


async def stream_anthropic(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    base_url: str,
    api_key: str,
    model: str,
) -> AsyncGenerator[dict, None]:
    """Stream from Anthropic's /v1/messages endpoint, yielding the same event
    shape as the OpenAI streaming path: `content_token`, `thinking_token`
    (extended-thinking deltas, display-only) and a single `final`.
    """
    headers, payload = _build_anthropic_request(messages, tools, temperature, model, api_key)

    state = _AnthropicStreamState()
    client = await _get_client()
    last_error: str | None = None
    max_retries = RETRY["llm"]["max_retries"]

    for attempt in range(max_retries + 1):
        try:
            await _rate_limit_acquire("anthropic")
            async with client.stream(
                "POST", f"{base_url}/messages", json=payload, headers=headers
            ) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    body_text = body[:500].decode("utf-8", errors="replace")
                    # #D109: retry on rate-limit/overload like the OpenAI path
                    # does. 529 is Anthropic's "Overloaded" code (not in the
                    # shared retryable set) — add it explicitly. Honour
                    # Retry-After when present, else exponential backoff.
                    retryable = response.status_code in (
                        RETRY["llm"]["retryable_status_codes"] | {529}
                    )
                    if retryable and not state.yielded_any and attempt < max_retries:
                        retry_after = parse_retry_after(response.headers.get("retry-after"))
                        backoff = retry_after if retry_after is not None else calc_backoff(attempt)
                        logger.warning(
                            "Anthropic HTTP %d (attempt %d/%d), retrying in %.1fs",
                            response.status_code, attempt + 1, max_retries + 1, backoff,
                        )
                        last_error = f"HTTP {response.status_code}"
                        await asyncio.sleep(backoff)
                        continue
                    # #D117: sanitize the body before logging (the OpenAI path
                    # already does — secrets echoed in the body went raw here).
                    logger.error(
                        "Anthropic HTTP %d: %s",
                        response.status_code, sanitize_for_log(body_text),
                    )
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [],
                        "error": f"HTTP {response.status_code}: {body_text[:200]}",
                    }
                    return

                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue

                    emit = _apply_anthropic_event(event, state)
                    if state.pending_thinking:
                        thought = "".join(state.pending_thinking)
                        state.pending_thinking.clear()
                        # Thinking is NOT "committed" content: a retry after a
                        # transient error can safely replay it, so it does not
                        # flip `yielded_any`.
                        yield {"type": "thinking_token", "token": thought}
                    if emit:
                        state.accumulated_content.add(emit)
                        state.yielded_any = True
                        yield {"type": "content_token", "token": emit}
                    if state.stopped:
                        break

            # Drain any unclosed `<…` tail held back during streaming (#505).
            tail = drain_dsml_tail(state.dsml_stripper, state.accumulated_content)
            if tail:
                state.yielded_any = True
                yield {"type": "content_token", "token": tail}

            # Success — break out of retry loop
            last_error = None
            break

        except _TRANSIENT_HTTPX_ERRORS as e:
            last_error = f"{type(e).__name__}: {e}"
            # Once any token has been yielded to the caller the partial stream
            # is already committed downstream — replaying would duplicate it.
            if state.yielded_any or attempt >= max_retries:
                break
            backoff = calc_backoff(attempt)
            logger.warning(
                "Anthropic transient error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, max_retries + 1, backoff, e,
            )
            # Reset block accumulator + stopped flag for the next attempt;
            # the already-yielded tokens are committed, accumulated_content
            # carries forward so the final event reflects what reached the user.
            # #D118: also reset the DSML stripper — a `<…` tail it held back
            # from attempt 1 (before any token was yielded) would otherwise be
            # flushed concatenated to attempt 2's content, duplicating text.
            state.blocks = {}
            state.stopped = False
            state.dsml_stripper = DsmlStripper()
            state.pending_thinking = []
            await asyncio.sleep(backoff)
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.error("Anthropic non-transient error: %s", e, exc_info=True)
            break

    if last_error:
        yield {
            "type": "final",
            "content": "",
            "tool_calls": [],
            "error": last_error,
        }
        return

    final: dict = {
        "type": "final",
        "content": state.accumulated_content.get(),
        "tool_calls": _collect_tool_calls(state.blocks),
        "error": None,
        "usage": state.last_usage or None,
    }
    thinking_blocks = _collect_thinking_blocks(state.blocks)
    if thinking_blocks:
        final["thinking_blocks"] = thinking_blocks
    yield final


# ── Provider registry adapter (H2 #7) ──────────────────────────
#
# The dispatcher in llm.py uses the ProviderProtocol signature
# `(messages, tools, temperature, *, provider)`. stream_anthropic takes
# explicit base_url/api_key/model/timeout for testability, so this
# thin wrapper resolves them from the active provider config and
# matches the protocol.


async def _stream_anthropic_provider(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    *,
    provider: str = "",
) -> AsyncGenerator[dict, None]:
    from .config import get_provider_config

    cfg = get_provider_config(provider)
    async for event in stream_anthropic(
        messages=messages,
        tools=tools,
        temperature=temperature,
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
    ):
        yield event


from .providers import register as _register
_register("anthropic", _stream_anthropic_provider)
