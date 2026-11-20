"""
LLM streaming client for Mythos.

Handles OpenAI-compatible chat completions with tool calling support.
Streams SSE responses from providers (DeepSeek, OpenAI, Grok, Ollama).
Includes retry with exponential backoff, jitter, and rate-limit handling.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
from collections.abc import AsyncGenerator

import httpx

from ._rate_limiter import acquire_llm_token as _rate_limit_acquire
from ._security_log import sanitize_for_log
from .config import (
    DEFAULT_PROVIDER,
    HTTPX_LIMITS_LLM,
    LLM_TIMEOUT,
    RETRY,
    base_provider,
    get_provider_config,
)

logger = logging.getLogger(__name__)

# Cap para reasoning content acumulado — evita que modelos verbosos
# inflacionem a sessao com raciocinio repetitivo (#054).
_REASONING_CONTENT_CAP = 50_000

# DSML/XML invoke blocks that DeepSeek (and similar reasoning models) emit as
# raw text when they "think" about tool calls before the structured tool_calls
# field arrives. Leaking these to the terminal is noisy; strip them from content.
# `</?` covers both opening and closing tags. `[^>]*` is fully permissive —
# earlier tighter forms missed exotic separators (fullwidth pipes, etc.).
# Keywords (DSML, invoke, parameter, tool_calls) are unlikely in real prose.
_DSML_RE = re.compile(r"</?[^>]*DSML[^>]*>", re.IGNORECASE)
_XML_INVOKE_RE = re.compile(
    r"</?[^>]*\b(invoke|parameter|tool_calls)\b[^>]*>",
    re.IGNORECASE,
)


def _strip_dsml(text: str) -> str:
    """Remove DSML and <invoke>/<parameter>/<tool_calls> tags from content."""
    text = _DSML_RE.sub("", text)
    text = _XML_INVOKE_RE.sub("", text)
    return text


class DsmlStripper:
    """Stream-safe DSML stripper.

    A naïve per-chunk `_strip_dsml(chunk)` misses tags split across SSE
    boundaries (e.g. ``<|DSM`` arrives, then ``L|tool_calls>``). This buffers
    any unclosed ``<…`` tail until the matching ``>`` arrives so the regex
    sees the full tag.
    """

    __slots__ = ("_buffer",)

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> str:
        """Append a streaming chunk, return safe-to-emit text."""
        if not chunk:
            return ""
        self._buffer += chunk
        last_lt = self._buffer.rfind("<")
        if last_lt != -1 and ">" not in self._buffer[last_lt:]:
            # Hold back the unclosed `<…` tail until its `>` arrives.
            emit = self._buffer[:last_lt]
            self._buffer = self._buffer[last_lt:]
        else:
            emit = self._buffer
            self._buffer = ""
        return _strip_dsml(emit) if (emit and last_lt != -1) else (emit or "")

    def flush(self) -> str:
        """Drain the buffer at end-of-stream."""
        out = _strip_dsml(self._buffer) if self._buffer else ""
        self._buffer = ""
        return out


def drain_dsml_tail(stripper: DsmlStripper, accumulated) -> str | None:
    """Fim-de-stream: descarrega o tail retido pelo stripper no acumulador.

    AUDIT_V2.3 #505: o idioma flush→add estava copiado entre `llm.py` e
    `llm_anthropic.py`. Retorna o tail (para o caller fazer o yield e seus
    flags próprios, ex. `yielded_any` no Anthropic) ou None se vazio.
    """
    tail = stripper.flush()
    if not tail:
        return None
    accumulated.add(tail)
    return tail


# ─── Retry / Rate-limit config ───
# Lido de config.RETRY["llm"] (#DM036).

# Smaller local models (Ollama-backed) hallucinate tool calls less often at
# lower temperatures. Politica vive como flag `low_temperature` em
# config._PROVIDERS — adicionar provider novo so requer setar a flag,
# sem editar este arquivo (#DM011).
_LOW_TEMPERATURE = 0.2

# Rolling cap for raw_content_for_recovery — see streaming loop for rationale.
_RAW_RECOVERY_CAP = 8192


# #026/#076: cliente httpx compartilhado por loop. Antes, cada call ao LLM
# criava `AsyncClient(...)` e fechava no fim, gastando 1 TLS handshake +
# nova conexao TCP por iteracao do agent loop (40+ por sessao tipica). O
# cliente persistido reusa keep-alive ate o servidor fechar a conexao.
# #DM042: migrado para LoopAwareClient (alpha/_http_singleton.py) — fonte
# unica de verdade compartilhada com llm_anthropic.py e web_search.py.
from ._http_singleton import LoopAwareClient

_shared_llm_client = LoopAwareClient(
    name="llm",
    build=lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(LLM_TIMEOUT, connect=10.0),
        limits=httpx.Limits(**HTTPX_LIMITS_LLM),
    ),
)


async def _get_shared_llm_client() -> httpx.AsyncClient:
    return await _shared_llm_client.get()


_DSML_INVOKE_RE = re.compile(
    r"<[^>]*?\binvoke\b[^>]*?\bname\s*=\s*\"([^\"]+)\"[^>]*>",
    re.IGNORECASE,
)
# Detects an actual `<invoke …>` or `<|DSML|invoke …>` opening tag in raw
# stream content — used by _build_final_with_recovery to flag drift
# without false-positives on prose containing the bare verb "invoke".
_DSML_OPEN_TAG_RE = re.compile(
    r"<\s*(?:\|\s*DSML\s*\|\s*)?invoke\b",
    re.IGNORECASE,
)
_DSML_PARAM_RE = re.compile(
    r"<[^>]*?\bparameter\b([^>]*)>(.*?)</[^>]*?\bparameter\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)
_DSML_ATTR_NAME_RE = re.compile(r'name="([^"]+)"', re.IGNORECASE)
_DSML_ATTR_STRING_RE = re.compile(r'string="(true|false)"', re.IGNORECASE)


def _finalize_recovered_call(name: str, args_str: str, id_prefix: str) -> dict | None:
    """Common tail for the two recovery paths: registry check + deterministic
    id (hash-of-input so repeated parses of the same payload yield the same
    id; `usedforsecurity=False` is the standard bandit/ruff B324 escape hatch
    — this is a tool-call id, not a cryptographic hash)."""
    from .tools import get_tool
    if get_tool(name) is None:
        logger.debug("Recovered tool call '%s' not in registry — discarding", name)
        return None
    digest = hashlib.sha1(
        (name + args_str).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:8]
    return {"id": f"call_{id_prefix}_{digest}", "name": name, "arguments": args_str}


def _recover_tool_call_from_dsml(content: str) -> dict | None:
    """Recover a tool call from <|DSML|invoke>/<|DSML|parameter> blocks.

    DeepSeek-V4-pro occasionally emits its tool calls as XML-like text
    blocks in the content stream instead of the structured tool_calls
    field. The user only sees the markup leak through; the tool never
    runs and the agent stalls waiting for an approval that never comes.
    This parser converts the markup back into a proper tool_call dict.

    AUDIT_V1.8 #020: finditer(content) varria o content inteiro,
    misturando params de invokes diferentes. content.find() limita
    cada invoke ao seu proprio escopo.
    """
    invoke_start = content.find("<|DSML|invoke")
    if invoke_start == -1:
        invoke_start = content.find("<invoke")
    if invoke_start == -1:
        return None
    invoke_end = content.find("</invoke>", invoke_start)
    if invoke_end == -1:
        invoke_end = content.find("/>", invoke_start)
    if invoke_end == -1:
        invoke_end = len(content)
    block = content[invoke_start:invoke_end]

    name_m = _DSML_INVOKE_RE.search(block)
    if not name_m:
        return None
    name = name_m.group(1)

    params: dict = {}
    for m in _DSML_PARAM_RE.finditer(block):
        attrs, value = m.group(1), m.group(2).strip()
        name_m = _DSML_ATTR_NAME_RE.search(attrs)
        if not name_m:
            continue
        key = name_m.group(1)
        str_m = _DSML_ATTR_STRING_RE.search(attrs)
        if str_m and str_m.group(1).lower() == "false":
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        params[key] = value
    if not params:
        return None
    return _finalize_recovered_call(name, json.dumps(params, ensure_ascii=False), "dsml")

def _recover_tool_call_from_content(content: str) -> dict | None:
    """Recover a tool call from a content string when the model emitted it as
    text instead of via the OpenAI ``tool_calls`` field.

    Some Ollama-served models (notably qwen2.5-coder) occasionally drift into
    code-completion mode and dump a tool call as a fenced JSON block. Returns
    a tool_call dict matching the streamed format, or None if recovery isn't
    safe/possible.
    """
    if not content:
        return None
    text = content.strip()
    if not text:
        return None

    # Strip a single ``` or ```json fence wrapping the whole content.
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl == -1:
            return None
        body = text[first_nl + 1 :]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        text = body.strip()

    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    # Accept the two common shapes models emit:
    #   {"name": "X", "arguments": {...}}
    #   {"function": {"name": "X", "arguments": {...}}}
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else {}
    name = obj.get("name") or fn.get("name")
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters")
    if args is None:
        args = fn.get("arguments")
    if not isinstance(name, str) or not name or args is None:
        return None

    if isinstance(args, dict):
        args_str = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str):
        args_str = args
    else:
        return None
    # DL028: discard names not in the registry — Ollama hallucinates these.
    return _finalize_recovered_call(name, args_str, "recovered")


# #300: httpx.ConnectError/ReadError/WriteError/RemoteProtocolError NÃO são
# subclasses de TimeoutException/ConnectionError/OSError — escapavam do handler
# de retry e crashavam o processo inteiro. Tratados como transitórios
# (retentáveis), espelhando o llm_anthropic. `httpx.TimeoutException` cobre
# todos os *Timeout (Read/Connect/Write/Pool).
_TRANSIENT_HTTPX_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
    ConnectionError,
    OSError,
)


def _calc_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Calculate backoff delay with exponential growth, jitter, capped at RETRY["llm"]["max_backoff"].

    Jitter prevents thundering herd: all clients back off at slightly different
    intervals instead of retrying in lockstep.
    """
    if retry_after is not None:
        # Add small jitter even to server-specified delays. Jitter pode
        # ate 1.2x o base, entao precisamos do `min(..., RETRY["llm"]["max_backoff"])`
        # explicito (#D023): sem ele, o resultado podia exceder o cap em
        # ate 20% (ex: 30s -> 36s) violando o invariante anunciado.
        base = min(retry_after, RETRY["llm"]["max_backoff"])
        return min(base * (0.8 + random.random() * 0.4), RETRY["llm"]["max_backoff"])
    delay = RETRY["llm"]["initial_backoff"] * (RETRY["llm"]["backoff_multiplier"] ** attempt)
    # Full jitter: uniform random between 0 and calculated delay
    jittered = delay * (0.5 + random.random() * 0.5)
    return min(jittered, RETRY["llm"]["max_backoff"])


# #DM072: alias público — llm_anthropic (e outros providers) compartilham o
# backoff; o nome underscore fica como retrocompat interna.
calc_backoff = _calc_backoff


# ─── stream_chat_with_tools helpers ───
#
# Pure helpers so the main coroutine focuses on I/O orchestration (HTTP
# request, retry loop, yield events) while parsing/recovery logic lives
# in units that don't touch the network and can be unit-tested directly.


class _StreamAccumulator:
    """String accumulator que evita O(N²) `+=` em content grande.

    AUDIT_V1.8 #042: `state.accumulated_content += chunk` em 400KB de
    response custa 50-200ms CPU (copia O(N) por chunk). Lista interna
    + join so no final corta pra O(total_len) no final.
    """

    __slots__ = ("_parts", "_len", "_pending_cap")

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._len = 0
        self._pending_cap: int | None = None

    def add(self, text: str) -> None:
        self._parts.append(text)
        self._len += len(text)

    def get(self) -> str:
        if self._pending_cap is not None and self._len > self._pending_cap:
            self._trim(self._pending_cap)
        return "".join(self._parts)

    def __bool__(self) -> bool:
        return self._len > 0

    def __len__(self) -> int:
        return self._len

    def _trim(self, max_len: int) -> None:
        joined = "".join(self._parts)
        self._parts = [joined[-max_len:]]
        self._len = max_len
        self._pending_cap = None

    def cap(self, max_len: int) -> None:
        """Drop oldest parts until total length fits max_len.

        O trim exato pode ficar pendente; `get()` o aplica antes de juntar,
        entao o resultado visivel e sempre o ultimo `max_len` do stream."""
        if self._len <= max_len:
            return
        self._pending_cap = max_len
        if self._len > max_len + (max_len >> 1):
            self._trim(max_len)


# #DM072: alias público — compartilhado com llm_anthropic; o nome underscore
# fica como retrocompat interna.
StreamAccumulator = _StreamAccumulator


class _StreamState:
    """Per-attempt accumulators for one streaming call.

    Held outside `stream_chat_with_tools` so the chunk processor and
    final-event builder are pure functions of state (no closures over
    the coroutine's locals).
    """

    __slots__ = (
        "accumulated_content",
        "raw_content_for_recovery",
        "accumulated_reasoning",
        "tool_calls_acc",
        "last_finish_reason",
        "last_usage",
        "dsml_stripper",
        "pending_thinking",
    )

    def __init__(self) -> None:
        self.accumulated_content = _StreamAccumulator()
        self.raw_content_for_recovery = ""
        self.accumulated_reasoning = _StreamAccumulator()
        # Reasoning deltas not yet yielded as `thinking_token` (drained by
        # the SSE loop after each chunk). Side-channel so `_process_chunk`
        # keeps returning only user-visible content.
        self.pending_thinking: list[str] = []
        self.tool_calls_acc: dict[int, dict] = {}
        # Capture the last non-empty finish_reason. Gemini emits diagnostic
        # values (e.g. "function_call_filter: MALFORMED_FUNCTION_CALL")
        # that surface to the user instead of an opaque "(turno encerrado)".
        self.last_finish_reason = ""
        # From the trailing `include_usage` chunk — forwarded to the agent
        # loop so it can pass to alpha.cost.record_usage.
        self.last_usage: dict | None = None
        self.dsml_stripper = DsmlStripper()


def _build_request(
    *,
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    api_key: str,
    model: str,
    supports_tools: bool,
    extra_payload: dict | None = None,
) -> tuple[dict, dict]:
    """Build (headers, payload) for the chat completions POST."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        # Ask the provider to emit a final SSE chunk with token usage.
        # OpenAI-compatible providers honor this; Anthropic uses its own
        # adapter (`llm_anthropic.py`) and has its own usage path.
        "stream_options": {"include_usage": True},
    }
    if tools and supports_tools:
        payload["tools"] = tools
    if extra_payload:
        payload.update(extra_payload)
    return headers, payload


class _ToolCallEntry(dict):
    """Entrada de tool_calls_acc: deltas de `arguments` acumulam numa lista
    de partes e o join acontece no primeiro acesso a `entry["arguments"]`."""

    __slots__ = ("_arg_parts",)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._arg_parts: list[str] = []

    def append_arguments(self, text: str) -> None:
        self._arg_parts.append(text)

    def __getitem__(self, key):
        if key == "arguments" and self._arg_parts:
            dict.__setitem__(
                self,
                "arguments",
                dict.__getitem__(self, "arguments") + "".join(self._arg_parts),
            )
            self._arg_parts.clear()
        return dict.__getitem__(self, key)


def _accumulate_tool_call_delta(tool_calls_acc: dict[int, dict], tc_delta: dict) -> None:
    """Apply one streaming tool_call delta to the accumulator.

    Google's OpenAI-compat layer omits the per-delta `index` field that
    OpenAI's own spec requires, so default to the next available slot
    when missing. Without this, every Gemini tool call raised KeyError
    and got dropped silently — the user saw a "(turno encerrado)" with
    no hint of why.
    """
    idx = tc_delta.get("index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = len(tool_calls_acc)
    if idx not in tool_calls_acc:
        tool_calls_acc[idx] = _ToolCallEntry({
            "id": tc_delta.get("id", ""),
            "name": (tc_delta.get("function") or {}).get("name", ""),
            "arguments": "",
            "extra_content": None,
        })
    entry = tool_calls_acc[idx]
    if tc_delta.get("id"):
        entry["id"] = tc_delta["id"]
    fn = tc_delta.get("function") or {}
    if fn.get("name"):
        entry["name"] = fn["name"]
    if fn.get("arguments"):
        entry.append_arguments(fn["arguments"])
    # Gemini's OpenAI-compat returns a `thought_signature` under
    # extra_content that MUST be echoed back on the next turn or the
    # API replies HTTP 400 INVALID_ARGUMENT.
    ec = tc_delta.get("extra_content")
    if ec:
        entry["extra_content"] = ec


def _process_chunk(data: dict, state: _StreamState) -> str | None:
    """Apply one parsed SSE chunk to `state`. Return any user-visible text
    to yield as a content_token, or None.
    """
    # OpenAI's `include_usage` mode emits a trailing chunk where `choices`
    # is empty and `usage` carries the token totals. Capture it and skip.
    if data.get("usage") and not data.get("choices"):
        state.last_usage = data["usage"]
        return None
    choices = data.get("choices") or []
    if not choices:
        return None
    choice = choices[0]
    # Some providers attach usage to the same chunk as the final choice
    # instead of a separate trailer.
    if data.get("usage"):
        state.last_usage = data["usage"]
    fr = choice.get("finish_reason")
    if fr:
        state.last_finish_reason = str(fr)
    delta = choice.get("delta") or {}

    emit: str | None = None
    # `dsml_stripper` buffers any unclosed `<…` tail so a tag split across
    # SSE chunks is still removed cleanly. raw_content_for_recovery is a
    # rolling 8KB tail kept only so end-of-stream DSML recovery has
    # something to parse — DSML blocks always land at the end and
    # accumulating the full stream would double memory on long responses
    # for a path that fires <1% of turns.
    content = delta.get("content", "")
    if content:
        if not state.tool_calls_acc:
            state.raw_content_for_recovery += content
            if len(state.raw_content_for_recovery) > _RAW_RECOVERY_CAP:
                state.raw_content_for_recovery = state.raw_content_for_recovery[-_RAW_RECOVERY_CAP:]
        safe = state.dsml_stripper.feed(content)
        if safe:
            state.accumulated_content.add(safe)
            emit = safe

    # Thinking tokens (DeepSeek-reasoner). Acumulados para voltar ao
    # provider (round-trip) E enfileirados em `pending_thinking` para o
    # loop SSE emitir como `thinking_token` (render dim na UI, nunca
    # entra no `content`). AUDIT_V1.2 #022: cap at 50KB per turn —
    # reasoning can reach 100KB+ and bloats context invisibly.
    reasoning = delta.get("reasoning_content", "")
    if reasoning:
        state.accumulated_reasoning.add(reasoning)
        state.accumulated_reasoning.cap(_REASONING_CONTENT_CAP)
        state.pending_thinking.append(reasoning)

    if delta.get("tool_calls"):
        for tc_delta in delta["tool_calls"]:
            _accumulate_tool_call_delta(state.tool_calls_acc, tc_delta)

    return emit


def _build_final_with_tool_calls(state: _StreamState) -> dict:
    """Build the success final event when the structured tool_calls field
    was populated during streaming.
    """
    tool_calls = [
        {
            "id": tc["id"],
            "name": tc["name"],
            "arguments": tc["arguments"],
            **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {}),
        }
        for _, tc in sorted(state.tool_calls_acc.items())
    ]
    return {
        "type": "final",
        "content": state.accumulated_content.get(),
        "tool_calls": tool_calls,
        "reasoning_content": state.accumulated_reasoning.get() or None,
        "error": None,
        "usage": state.last_usage,
    }


def _build_final_with_recovery(state: _StreamState, provider: str, model_name: str) -> dict:
    """Build the final event when tool_calls_acc stayed empty.

    Fallback chain: first try DSML/XML invoke blocks (DeepSeek-V4-pro),
    then fenced JSON (Ollama qwen-coder). DSML uses the raw un-stripped
    buffer because the live sanitizer already erased the markup from
    accumulated_content. If nothing recovers AND the provider gave a
    diagnostic finish_reason, surface that as an error so the user sees
    why the turn went silent.
    """
    reasoning_out = state.accumulated_reasoning.get() or None
    recovered = (
        _recover_tool_call_from_dsml(state.raw_content_for_recovery)
        or _recover_tool_call_from_content(state.accumulated_content.get())
    )
    if recovered is not None:
        logger.info(
            "Recovered tool call '%s' from content (provider=%s)",
            recovered["name"], provider,
        )
        return {
            "type": "final",
            "content": "",
            "tool_calls": [recovered],
            "reasoning_content": reasoning_out,
            "error": None,
            "usage": state.last_usage,
        }

    # If the raw buffer carried DSML markup but neither recoverer could
    # turn it into a real tool_call, log a warning AND flag the final
    # event so the agent loop can inject a corrective user message
    # ("emit tool_calls via API, not <invoke> markup") instead of
    # ending the turn silently.
    #
    # CODE_REVIEW_FASE1 #1: detect actual <invoke …> opening tags rather
    # than the substring "invoke" — the latter matched legitimate prose
    # like "I'll invoke the API" and hijacked normal replies into a
    # spurious corrective re-prompt loop. The regex matches `<invoke>`
    # and `<|DSML|invoke …>`, the two shapes producers actually emit.
    dsml_recovery_failed = False
    raw = state.raw_content_for_recovery
    if raw and _DSML_OPEN_TAG_RE.search(raw):
        logger.warning(
            "DSML/invoke markup detected in content but recovery failed "
            "(provider=%s, len=%d) — flagging for corrective re-prompt",
            provider, len(raw),
        )
        dsml_recovery_failed = True

    # If the stream produced nothing visible AND the provider gave a
    # diagnostic finish_reason (anything other than the normal
    # "stop"/"length"), surface it as an error. Gemini's
    # "function_call_filter: MALFORMED_FUNCTION_CALL" is canonical.
    fr_lower = state.last_finish_reason.lower()
    benign = fr_lower in ("", "stop", "length", "end_turn")
    if not state.accumulated_content and not benign:
        logger.warning(
            "Empty stream with diagnostic finish_reason (provider=%s, model=%s): %s",
            provider, model_name, state.last_finish_reason,
        )
        return {
            "type": "final",
            "content": "",
            "tool_calls": [],
            "reasoning_content": reasoning_out,
            "error": f"empty response (finish_reason: {state.last_finish_reason})",
            "dsml_recovery_failed": dsml_recovery_failed,
        }
    return {
        "type": "final",
        "content": state.accumulated_content.get(),
        "tool_calls": [],
        "reasoning_content": reasoning_out,
        "error": None,
        "usage": state.last_usage,
        "dsml_recovery_failed": dsml_recovery_failed,
    }


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse the Retry-After header value (seconds form only)."""
    if not header_value:
        return None
    try:
        return max(0.0, min(float(header_value), 3600.0))  # cap at 1h (#025)
    except ValueError:
        return None


# #DM072: alias público — compartilhado com llm_anthropic; o nome underscore
# fica como retrocompat interna.
parse_retry_after = _parse_retry_after


async def stream_chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    temperature: float = 0.5,
    provider: str | None = None,
) -> AsyncGenerator[dict, None]:
    """
    Stream LLM response with tool calling support.

    Includes retry with exponential backoff for transient errors (429, 5xx).
    Respects Retry-After headers from rate-limited responses.

    `provider=None` resolves to `config.DEFAULT_PROVIDER` (#DM071 — same
    pattern as `sdk.run_task`).

    Yields events:
    - {"type": "content_token", "token": "..."}  — incremental text
    - {"type": "thinking_token", "token": "..."} — reasoning deltas
      (display-only; the round-trip copy lives in `final.reasoning_content`)
    - {"type": "final", "content": "...", "tool_calls": [...], "error": None}
    """
    if provider is None:
        provider = DEFAULT_PROVIDER
    cfg = get_provider_config(provider)
    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    model = cfg["model"]
    supports_tools = cfg["supports_tools"]
    api_format = cfg.get("api_format", "openai")

    if cfg.get("low_temperature") and temperature > _LOW_TEMPERATURE:
        temperature = _LOW_TEMPERATURE

    # Non-OpenAI formats: dispatch through the provider registry.
    # Importing the provider module is what triggers self-registration —
    # we only import the ones we might actually need (lazy by format key)
    # to keep startup time on the default OpenAI path untouched.
    # Adding Gemini-native or Bedrock-Converse is one new file in
    # alpha/providers/ plus one `register(name, impl)` call there.
    if api_format != "openai":
        if api_format == "anthropic":
            from . import llm_anthropic  # noqa: F401 — triggers registration
        from .providers import get as _get_provider_impl
        impl = _get_provider_impl(api_format)
        if impl is not None:
            tools_to_send = tools if tools and supports_tools else []
            async for event in impl(
                messages, tools_to_send, temperature, provider=provider
            ):
                yield event
            return
        # Unregistered format: fall through to OpenAI compat. Most third-
        # party providers use OpenAI dialect under the hood (DeepSeek,
        # Grok, Ollama, Gemini's compat layer), so this is the right
        # default rather than a hard error.

    headers, payload = _build_request(
        messages=messages,
        tools=tools,
        temperature=temperature,
        api_key=api_key,
        model=model,
        supports_tools=supports_tools,
        extra_payload=cfg.get("extra_payload"),
    )

    last_error = None
    max_retries = RETRY["llm"]["max_retries"]

    for attempt in range(max_retries + 1):
        state = _StreamState()

        try:
            client = await _get_shared_llm_client()
            await _rate_limit_acquire(base_provider(provider))
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                # Handle retryable HTTP errors
                if response.status_code in RETRY["llm"]["retryable_status_codes"]:
                    await response.aread()  # drain so the connection can be reused
                    last_error = f"HTTP {response.status_code}"

                    if attempt < max_retries:
                        retry_after = _parse_retry_after(response.headers.get("retry-after"))
                        delay = _calc_backoff(attempt, retry_after)
                        logger.warning(
                            "LLM %s (attempt %d/%d), retrying in %.1fs",
                            last_error, attempt + 1, max_retries + 1, delay,
                        )
                        if state.accumulated_content:
                            yield {"type": "stream_reset", "reason": last_error}
                        await asyncio.sleep(delay)
                        continue

                    # Max retries exhausted
                    logger.error("LLM %s after %d attempts", last_error, max_retries + 1)
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [],
                        "error": f"{last_error} after {max_retries + 1} attempts",
                    }
                    return

                # Non-retryable HTTP error
                if response.status_code >= 400:
                    error_body = await response.aread()
                    # Some providers echo back the request (incl. Authorization
                    # header) in error responses — sanitize before logging.
                    body_str = error_body.decode("utf-8", errors="replace")
                    logger.error(
                        "LLM HTTP %d: %s",
                        response.status_code, sanitize_for_log(body_str, max_chars=500),
                    )
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [],
                        "error": f"HTTP error {response.status_code}",
                    }
                    return

                # Stream response
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue  # Expected for non-JSON SSE lines
                    if not isinstance(data, dict):
                        continue  # heartbeat/keepalive: data: null / "ping" / 0 / []
                    try:
                        emit = _process_chunk(data, state)
                    except (KeyError, IndexError, AttributeError, TypeError) as e:
                        logger.debug("Unexpected SSE chunk format: %s | data: %s", e, data_str[:200])
                        continue
                    if state.pending_thinking:
                        thought = "".join(state.pending_thinking)
                        state.pending_thinking.clear()
                        yield {"type": "thinking_token", "token": thought}
                    if emit:
                        yield {"type": "content_token", "token": emit}

            # Drain any unclosed `<…` tail held back during streaming (#505).
            tail = drain_dsml_tail(state.dsml_stripper, state.accumulated_content)
            if tail:
                yield {"type": "content_token", "token": tail}

            # Success — build final event and return.
            if state.tool_calls_acc:
                yield _build_final_with_tool_calls(state)
            else:
                yield _build_final_with_recovery(state, provider, cfg.get("model", ""))
            return  # success, no retry

        except _TRANSIENT_HTTPX_ERRORS as e:
            # AUDIT_V1.10 #050: handlers TimeoutException e ConnectionError
            # tinham ~15 linhas idênticas de retry/final. Unificados com
            # dispatch por isinstance — o bloco de retry e o yield final
            # são compartilhados.
            if isinstance(e, httpx.TimeoutException):
                last_error = f"LLM timeout ({LLM_TIMEOUT}s)"
            else:
                last_error = f"Connection error: {e}"

            if attempt < max_retries:
                delay = _calc_backoff(attempt)
                logger.warning(
                    "%s (attempt %d/%d), retrying in %.1fs",
                    last_error, attempt + 1, max_retries + 1, delay,
                )
                if state.accumulated_content:
                    yield {"type": "stream_reset", "reason": last_error}
                await asyncio.sleep(delay)
                continue

            logger.error("%s after %d attempts", last_error, max_retries + 1)
            yield {
                "type": "final",
                "content": state.accumulated_content.get(),
                "tool_calls": [],
                "error": f"{last_error} after {max_retries + 1} attempts",
            }
            return

        except (json.JSONDecodeError, KeyError, ValueError, RuntimeError) as e:
            logger.error("LLM error: %s", e)
            yield {
                "type": "final",
                "content": state.accumulated_content.get(),
                "tool_calls": [],
                "error": str(e),
            }
            return

        except Exception as e:
            # #300: rede blindada — qualquer exceção não-prevista (ex.: um erro
            # httpx novo) degrada para um `final` com erro em vez de crashar o
            # processo. CancelledError é BaseException, não Exception → propaga.
            logger.error("LLM unexpected error: %s: %s", type(e).__name__, e)
            yield {
                "type": "final",
                "content": state.accumulated_content.get(),
                "tool_calls": [],
                "error": f"{type(e).__name__}: {e}",
            }
            return
