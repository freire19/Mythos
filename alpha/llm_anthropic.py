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
import random
from collections.abc import AsyncGenerator

import httpx

from ._http_singleton import LoopAwareClient
from ._rate_limiter import acquire_llm_token as _rate_limit_acquire

from .config import HTTPX_LIMITS_LLM, LLM_TIMEOUT, RETRY
from .llm import DsmlStripper, _calc_backoff

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 8192
_TRANSIENT_HTTPX_ERRORS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
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


async def _get_client(timeout: float | None = None) -> httpx.AsyncClient:
    """Return the loop-aware shared httpx.AsyncClient for Anthropic.

    `timeout` parameter kept for back-compat; ignored (build callable usa
    LLM_TIMEOUT). Per-request override deve ir via client.request(timeout=)."""
    return await _client.get()


# ── OpenAI → Anthropic conversion ──


def _convert_tools(openai_tools: list[dict]) -> list[dict]:
    """Convert OpenAI function-tool schema to Anthropic tool schema."""
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
            url = (b.get("image_url") or {}).get("url", "")
            if url.startswith(_DATA_URL_PREFIX):
                # data:<media_type>;base64,<data>
                head, _, data = url.partition(",")
                media_type = head[len(_DATA_URL_PREFIX):].split(";", 1)[0] or "image/png"
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    }
                )
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks if blocks else ""


def _convert_messages(openai_messages: list[dict]) -> tuple[str, list[dict]]:
    """Split system messages out and convert the rest to Anthropic shape.

    Adjacent tool-result messages are coalesced into a single `user` turn
    with multiple `tool_result` content blocks (Anthropic requires this).
    """
    system_parts: list[str] = []
    converted: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results():
        if pending_tool_results:
            converted.append({"role": "user", "content": list(pending_tool_results)})
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
            converted.append({"role": "user", "content": _convert_user_content(content)})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
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
    )

    def __init__(self) -> None:
        self.accumulated_content = ""
        self.blocks: dict[int, dict] = {}
        self.dsml_stripper = DsmlStripper()
        # Anthropic emits input_tokens in `message_start` and final
        # output_tokens in `message_delta`. Both stay in the same dict
        # shape so alpha.cost.record_usage handles both providers
        # without per-provider branches.
        self.last_usage: dict = {}
        self.yielded_any = False
        self.stopped = False


def _build_anthropic_request(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    model: str,
    api_key: str,
) -> tuple[dict, dict]:
    """Build (headers, payload) for the Anthropic /v1/messages POST."""
    system_text, anthropic_messages = _convert_messages(messages)
    anthropic_tools = _convert_tools(tools)
    payload: dict = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "messages": anthropic_messages,
        "stream": True,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text
    if anthropic_tools:
        payload["tools"] = anthropic_tools
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
        idx = event["index"]
        block = event["content_block"]
        if block["type"] == "text":
            state.blocks[idx] = {"type": "text", "text": ""}
        elif block["type"] == "tool_use":
            state.blocks[idx] = {
                "type": "tool_use",
                "id": block.get("id", ""),
                "name": block.get("name", ""),
                "input_json": "",
            }

    elif etype == "content_block_delta":
        idx = event["index"]
        delta = event.get("delta", {})
        block = state.blocks.get(idx)
        if block is None:
            return None
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
                block["text"] = block.get("text", "") + text
                safe = state.dsml_stripper.feed(text)
                if safe:
                    return safe
        elif delta.get("type") == "input_json_delta":
            block["input_json"] = block.get("input_json", "") + delta.get("partial_json", "")

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
        args_json = block.get("input_json", "") or "{}"
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


async def stream_anthropic(
    messages: list[dict],
    tools: list[dict],
    temperature: float,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> AsyncGenerator[dict, None]:
    """Stream from Anthropic's /v1/messages endpoint, yielding the same event
    shape as the OpenAI streaming path: `content_token` and a single `final`.
    """
    headers, payload = _build_anthropic_request(messages, tools, temperature, model, api_key)

    state = _AnthropicStreamState()
    client = await _get_client(timeout)
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
                    logger.error("Anthropic HTTP %d: %s", response.status_code, body[:500])
                    yield {
                        "type": "final",
                        "content": "",
                        "tool_calls": [],
                        "error": f"HTTP {response.status_code}: {body[:200].decode('utf-8', errors='replace')}",
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
                    if emit:
                        state.accumulated_content += emit
                        state.yielded_any = True
                        yield {"type": "content_token", "token": emit}
                    if state.stopped:
                        break

            # Drain any unclosed `<…` tail held back during streaming.
            tail = state.dsml_stripper.flush()
            if tail:
                state.accumulated_content += tail
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
            backoff = _calc_backoff(attempt)
            logger.warning(
                "Anthropic transient error (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, max_retries + 1, backoff, e,
            )
            # Reset block accumulator + stopped flag for the next attempt;
            # the already-yielded tokens are committed, accumulated_content
            # carries forward so the final event reflects what reached the user.
            state.blocks = {}
            state.stopped = False
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

    yield {
        "type": "final",
        "content": state.accumulated_content,
        "tool_calls": _collect_tool_calls(state.blocks),
        "error": None,
        "usage": state.last_usage or None,
    }


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
    from .config import LLM_TIMEOUT, get_provider_config

    cfg = get_provider_config(provider)
    async for event in stream_anthropic(
        messages=messages,
        tools=tools,
        temperature=temperature,
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        timeout=LLM_TIMEOUT,
    ):
        yield event


from .providers import register as _register
_register("anthropic", _stream_anthropic_provider)
