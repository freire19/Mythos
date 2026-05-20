"""
LLM streaming client for Alpha Code.

Handles OpenAI-compatible chat completions with tool calling support.
Streams SSE responses from providers (DeepSeek, OpenAI, Grok, Ollama).
Includes retry with exponential backoff, jitter, and rate-limit handling.
"""

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
from .config import HTTPX_LIMITS_LLM, LLM_TIMEOUT, RETRY, get_provider_config

logger = logging.getLogger(__name__)

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
        return _strip_dsml(emit) if emit else ""

    def flush(self) -> str:
        """Drain the buffer at end-of-stream."""
        out = _strip_dsml(self._buffer) if self._buffer else ""
        self._buffer = ""
        return out

# ─── Retry / Rate-limit config ───

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
    """
    invoke_match = _DSML_INVOKE_RE.search(content)
    if not invoke_match:
        return None
    name = invoke_match.group(1)

    params: dict = {}
    for m in _DSML_PARAM_RE.finditer(content):
        attrs, value = m.group(1), m.group(2).strip()
        name_m = _DSML_ATTR_NAME_RE.search(attrs)
        if not name_m:
            continue
        key = name_m.group(1)
        str_m = _DSML_ATTR_STRING_RE.search(attrs)
        # string="false" means the value is JSON-encoded (e.g. an array).
        # Default to treating as string when the flag is missing.
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


async def stream_chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    temperature: float = 0.5,
    provider: str = "deepseek",
) -> AsyncGenerator[dict, None]:
    """
    Stream LLM response with tool calling support.

    Includes retry with exponential backoff for transient errors (429, 5xx).
    Respects Retry-After headers from rate-limited responses.

    Yields events:
    - {"type": "content_token", "token": "..."}  — incremental text
    - {"type": "final", "content": "...", "tool_calls": [...], "error": None}
    """
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

    last_error = None

    for attempt in range(RETRY["llm"]["max_retries"] + 1):
        accumulated_content = ""
        raw_content_for_recovery = ""
        dsml_stripper = DsmlStripper()
        # `reasoning_content` e o canal de "thinking" do DeepSeek-reasoner
        # (e tambem dos `gpt-oss` no Ollama). A API exige que o campo
        # acumulado da resposta seja devolvido na turn seguinte, ou
        # responde HTTP 400 "The `reasoning_content` in the thinking mode
        # must be passed back to the API." Sem isso, qualquer iteracao
        # com tool_call quebra. Provedores que nao usam thinking simplesmente
        # nunca emitem o campo, entao guardamos None e nao adicionamos
        # ao message dict.
        accumulated_reasoning = ""
        tool_calls_acc: dict[int, dict] = {}
        # Capture the last non-empty finish_reason. Gemini and similar
        # providers sometimes return an empty stream with a diagnostic
        # finish_reason (e.g. "function_call_filter: MALFORMED_FUNCTION_CALL")
        # — without this, the agent loop sees an empty turn and prints
        # "(turno encerrado)" with no hint of what went wrong.
        last_finish_reason = ""
        # Captured from the trailing usage chunk when stream_options.include_usage
        # is set. Forwarded in the final event so the agent loop can pass it
        # to alpha.cost.record_usage.
        last_usage: dict | None = None

        try:
            client = await _get_shared_llm_client()
            await _rate_limit_acquire(provider)
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                # Handle retryable HTTP errors
                if response.status_code in RETRY["llm"]["retryable_status_codes"]:
                    error_body = await response.aread()
                    last_error = f"HTTP {response.status_code}"

                    if attempt < RETRY["llm"]["max_retries"]:
                        # Parse Retry-After header for rate limits
                        retry_after = None
                        ra_header = response.headers.get("retry-after")
                        if ra_header:
                            try:
                                retry_after = float(ra_header)
                            except ValueError:
                                pass

                        delay = _calc_backoff(attempt, retry_after)
                        logger.warning(
                            f"LLM {last_error} (attempt {attempt + 1}/{RETRY["llm"]["max_retries"] + 1}), "
                            f"retrying in {delay:.1f}s"
                        )
                        if accumulated_content:
                            yield {"type": "stream_reset", "reason": last_error}
                        await asyncio.sleep(delay)
                        continue

                    # Max retries exhausted
                    logger.error(f"LLM {last_error} after {RETRY["llm"]["max_retries"] + 1} attempts")
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [],
                        "error": f"{last_error} after {RETRY["llm"]["max_retries"] + 1} attempts",
                    }
                    return

                # Non-retryable HTTP error
                if response.status_code >= 400:
                    error_body = await response.aread()
                    # Some providers echo back the request (incl. Authorization
                    # header) in error responses — sanitize before logging.
                    body_str = error_body.decode("utf-8", errors="replace")
                    logger.error(
                        f"LLM HTTP {response.status_code}: "
                        f"{sanitize_for_log(body_str, max_chars=500)}"
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
                        # OpenAI's `include_usage` mode emits a trailing chunk
                        # where `choices` is empty and `usage` carries the
                        # token totals. Capture it and skip the choice path.
                        if data.get("usage") and not data.get("choices"):
                            last_usage = data["usage"]
                            continue
                        choices = data.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        # Some providers attach usage to the same chunk as
                        # the final choice instead of a separate trailer.
                        if data.get("usage"):
                            last_usage = data["usage"]
                        fr = choice.get("finish_reason")
                        if fr:
                            last_finish_reason = str(fr)
                        delta = choice.get("delta", {})

                        # `dsml_stripper` buffers any unclosed `<…` tail so a
                        # tag split across SSE chunks is still removed cleanly.
                        # raw_content_for_recovery is a rolling 8KB tail kept
                        # only so end-of-stream DSML recovery has something
                        # to parse — DSML blocks always land at the end and
                        # accumulating the full stream would double memory
                        # on long responses for a path that fires <1% of turns.
                        content = delta.get("content", "")
                        if content:
                            if not tool_calls_acc:
                                raw_content_for_recovery += content
                                if len(raw_content_for_recovery) > _RAW_RECOVERY_CAP:
                                    raw_content_for_recovery = raw_content_for_recovery[-_RAW_RECOVERY_CAP:]
                            safe = dsml_stripper.feed(content)
                            if safe:
                                accumulated_content += safe
                                yield {"type": "content_token", "token": safe}

                        # Thinking tokens (DeepSeek-reasoner). Acumulados
                        # silenciosamente — nao streamamos para o usuario
                        # porque o formato e ruidoso e nao reflete a
                        # resposta final, mas precisam voltar pro provider.
                        # AUDIT_V1.2 #022: cap at 50KB per turn — reasoning
                        # can reach 100KB+ and bloats context invisibly.
                        reasoning = delta.get("reasoning_content", "")
                        if reasoning:
                            accumulated_reasoning += reasoning
                            if len(accumulated_reasoning) > 50_000:
                                accumulated_reasoning = accumulated_reasoning[-50_000:]

                        # Tool calls (streamed incrementally). Google's
                        # OpenAI-compat layer omits the per-delta `index`
                        # field that OpenAI's own spec requires, so default
                        # to the next available slot when missing. Without
                        # this, every Gemini tool call raised KeyError and
                        # got dropped silently — the user saw a "(turno
                        # encerrado)" with no hint of why.
                        if delta.get("tool_calls"):
                            for tc_delta in delta["tool_calls"]:
                                idx = tc_delta.get("index")
                                if idx is None:
                                    idx = len(tool_calls_acc)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {
                                        "id": tc_delta.get("id", ""),
                                        "name": tc_delta.get("function", {}).get(
                                            "name", ""
                                        ),
                                        "arguments": "",
                                        "extra_content": None,
                                    }
                                entry = tool_calls_acc[idx]
                                if tc_delta.get("id"):
                                    entry["id"] = tc_delta["id"]
                                fn = tc_delta.get("function", {})
                                if fn.get("name"):
                                    entry["name"] = fn["name"]
                                if fn.get("arguments"):
                                    entry["arguments"] += fn["arguments"]
                                # Gemini's OpenAI-compat returns a
                                # `thought_signature` under extra_content
                                # that MUST be echoed back on the next turn
                                # or the API replies HTTP 400 INVALID_ARGUMENT.
                                ec = tc_delta.get("extra_content")
                                if ec:
                                    entry["extra_content"] = ec

                    except json.JSONDecodeError:
                        continue  # Expected for non-JSON SSE lines
                    except (KeyError, IndexError) as e:
                        logger.debug(f"Unexpected SSE chunk format: {e} | data: {data_str[:200]}")
                        continue

            # Drain any unclosed `<…` tail held back during streaming.
            tail = dsml_stripper.flush()
            if tail:
                accumulated_content += tail
                yield {"type": "content_token", "token": tail}

            # Success — build final event and return
            reasoning_out = accumulated_reasoning or None
            if tool_calls_acc:
                tool_calls = [
                    {
                        "id": tc["id"],
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                        **({"extra_content": tc["extra_content"]} if tc.get("extra_content") else {}),
                    }
                    for _, tc in sorted(tool_calls_acc.items())
                ]
                yield {
                    "type": "final",
                    "content": accumulated_content,
                    "tool_calls": tool_calls,
                    "reasoning_content": reasoning_out,
                    "error": None,
                    "usage": last_usage,
                }
            else:
                # Fallback chain when the structured tool_calls field stayed
                # empty: first try DSML/XML invoke blocks (DeepSeek-V4-pro),
                # then fenced JSON (Ollama qwen-coder). DSML uses the raw
                # un-stripped buffer because the live sanitizer already
                # erased the markup from accumulated_content.
                recovered = (
                    _recover_tool_call_from_dsml(raw_content_for_recovery)
                    or _recover_tool_call_from_content(accumulated_content)
                )
                if recovered is not None:
                    logger.info(
                        f"Recovered tool call '{recovered['name']}' from content "
                        f"(provider={provider})"
                    )
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [recovered],
                        "reasoning_content": reasoning_out,
                        "error": None,
                        "usage": last_usage,
                    }
                else:
                    # If the raw buffer carried DSML markup but neither
                    # recoverer could turn it into a real tool_call, log a
                    # warning. Otherwise the turn ends silently and the user
                    # sees a blank prompt without knowing the model emitted
                    # un-parseable markup.
                    if raw_content_for_recovery and "invoke" in raw_content_for_recovery.lower():
                        logger.warning(
                            "DSML/invoke markup detected in content but recovery "
                            "failed (provider=%s, len=%d) — turn will end silent",
                            provider, len(raw_content_for_recovery),
                        )
                    # If the stream produced nothing visible AND the
                    # provider gave us a diagnostic finish_reason (anything
                    # other than the normal "stop"/"length"), surface it as
                    # an error so the user sees why the turn went silent.
                    # Gemini's "function_call_filter: MALFORMED_FUNCTION_CALL"
                    # is the canonical example.
                    fr_lower = last_finish_reason.lower()
                    benign = fr_lower in ("", "stop", "length", "end_turn")
                    if (not accumulated_content and not benign):
                        logger.warning(
                            "Empty stream with diagnostic finish_reason "
                            "(provider=%s, model=%s): %s",
                            provider, cfg.get("model", ""), last_finish_reason,
                        )
                        yield {
                            "type": "final",
                            "content": "",
                            "tool_calls": [],
                            "reasoning_content": reasoning_out,
                            "error": f"empty response (finish_reason: {last_finish_reason})",
                        }
                    else:
                        yield {
                            "type": "final",
                            "content": accumulated_content,
                            "tool_calls": [],
                            "reasoning_content": reasoning_out,
                            "error": None,
                            "usage": last_usage,
                        }
            return  # success, no retry

        except httpx.TimeoutException:
            last_error = f"LLM timeout ({LLM_TIMEOUT}s)"
            if attempt < RETRY["llm"]["max_retries"]:
                delay = _calc_backoff(attempt)
                logger.warning(
                    f"{last_error} (attempt {attempt + 1}/{RETRY["llm"]["max_retries"] + 1}), "
                    f"retrying in {delay:.1f}s"
                )
                if accumulated_content:
                    yield {"type": "stream_reset", "reason": last_error}
                await asyncio.sleep(delay)
                continue

            logger.error(f"{last_error} after {RETRY["llm"]["max_retries"] + 1} attempts")
            yield {
                "type": "final",
                "content": accumulated_content,
                "tool_calls": [],
                "error": f"{last_error} after {RETRY["llm"]["max_retries"] + 1} attempts",
            }
            return

        # Nota: httpx.HTTPStatusError nao e capturado porque `client.stream`
        # NAO chama `raise_for_status()` automaticamente — o status_code
        # >= 400 e tratado inline no caminho principal (linhas ~190 e
        # ~226). Manter um handler aqui era codigo morto (#052).

        except (ConnectionError, OSError) as e:
            last_error = f"Connection error: {e}"
            if attempt < RETRY["llm"]["max_retries"]:
                delay = _calc_backoff(attempt)
                logger.warning(
                    f"{last_error} (attempt {attempt + 1}/{RETRY["llm"]["max_retries"] + 1}), "
                    f"retrying in {delay:.1f}s"
                )
                if accumulated_content:
                    yield {"type": "stream_reset", "reason": last_error}
                await asyncio.sleep(delay)
                continue

            logger.error(f"{last_error} after {RETRY["llm"]["max_retries"] + 1} attempts")
            yield {
                "type": "final",
                "content": accumulated_content,
                "tool_calls": [],
                "error": last_error,
            }
            return

        except (json.JSONDecodeError, KeyError, ValueError, RuntimeError) as e:
            logger.error(f"LLM error: {e}")
            yield {
                "type": "final",
                "content": accumulated_content,
                "tool_calls": [],
                "error": str(e),
            }
            return
