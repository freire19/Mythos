"""Tests for the Anthropic API adapter (message + tool conversion)."""

import json

import pytest

from alpha.llm_anthropic import _convert_messages, _convert_tools


# ── Tool schema conversion ──


class TestConvertTools:
    def test_function_wrapped_form(self):
        openai = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        out = _convert_tools(openai)
        assert out == [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        ]

    def test_skips_invalid(self):
        assert _convert_tools([{"type": "weird"}, {}, "string"]) == []

    def test_missing_parameters_uses_empty_schema(self):
        openai = [{"type": "function", "function": {"name": "x", "description": "y"}}]
        out = _convert_tools(openai)
        assert out[0]["input_schema"] == {"type": "object", "properties": {}}


# ── Message conversion ──


class TestConvertMessages:
    def test_system_extracted(self):
        sys, msgs = _convert_messages([
            {"role": "system", "content": "You are X"},
            {"role": "user", "content": "hi"},
        ])
        assert sys == "You are X"
        assert msgs == [{"role": "user", "content": "hi"}]

    def test_multiple_system_messages_joined(self):
        sys, _ = _convert_messages([
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "hi"},
        ])
        assert sys == "A\n\nB"

    def test_assistant_text_only(self):
        _, msgs = _convert_messages([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        assert msgs[1] == {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}],
        }

    def test_assistant_with_tool_calls(self):
        _, msgs = _convert_messages([
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "I'll check.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"/x"}'},
                    }
                ],
            },
        ])
        blocks = msgs[1]["content"]
        assert blocks[0] == {"type": "text", "text": "I'll check."}
        assert blocks[1] == {
            "type": "tool_use",
            "id": "call_1",
            "name": "read_file",
            "input": {"path": "/x"},
        }

    def test_assistant_with_only_tool_calls_no_text(self):
        _, msgs = _convert_messages([
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}
                ],
            },
        ])
        blocks = msgs[1]["content"]
        assert len(blocks) == 1
        assert blocks[0]["type"] == "tool_use"

    def test_tool_results_coalesced_into_user_turn(self):
        # Anthropic requires consecutive tool_results to share one user message.
        _, msgs = _convert_messages([
            {"role": "user", "content": "do two things"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result-a"},
            {"role": "tool", "tool_call_id": "c2", "content": "result-b"},
            {"role": "user", "content": "next"},
        ])
        # Index 2 should be the merged user turn: both tool_results FIRST
        # (API requirement), then the following user text — consecutive
        # user turns are merged since Etapa 1 (vision tool results), which
        # appends a user message right after the batch's tool messages.
        merged = msgs[2]
        assert merged["role"] == "user"
        assert isinstance(merged["content"], list)
        assert len(merged["content"]) == 3
        assert merged["content"][0]["tool_use_id"] == "c1"
        assert merged["content"][0]["content"] == "result-a"
        assert merged["content"][1]["tool_use_id"] == "c2"
        assert merged["content"][2] == {"type": "text", "text": "next"}
        # No consecutive-user turn is emitted anymore
        assert len(msgs) == 3

    def test_malformed_tool_args_default_to_empty_dict(self):
        _, msgs = _convert_messages([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "x", "arguments": "not json"}}
                ],
            },
        ])
        assert msgs[0]["content"][0]["input"] == {}

    def test_non_string_tool_content_serialized(self):
        _, msgs = _convert_messages([
            {"role": "tool", "tool_call_id": "c1", "content": {"k": "v"}},
        ])
        assert msgs[0]["content"][0]["content"] == json.dumps({"k": "v"}, ensure_ascii=False)


# ── #D102: defensive event handling (malformed events must not crash) ──

class TestApplyEventDefensive:
    def _state(self):
        from alpha.llm_anthropic import _AnthropicStreamState
        return _AnthropicStreamState()

    def test_content_block_start_without_index_is_ignored(self):
        from alpha.llm_anthropic import _apply_anthropic_event
        st = self._state()
        # #D102: a malformed event missing `index`/`content_block` must
        # return None instead of raising KeyError (which the generic except
        # would treat as non-transient, killing the turn with no retry).
        assert _apply_anthropic_event({"type": "content_block_start"}, st) is None
        assert st.blocks == {}

    def test_content_block_start_without_type(self):
        from alpha.llm_anthropic import _apply_anthropic_event
        st = self._state()
        out = _apply_anthropic_event(
            {"type": "content_block_start", "index": 0, "content_block": {}}, st
        )
        assert out is None
        assert st.blocks == {}

    def test_content_block_delta_without_index_is_ignored(self):
        from alpha.llm_anthropic import _apply_anthropic_event
        st = self._state()
        assert _apply_anthropic_event({"type": "content_block_delta"}, st) is None

    def test_well_formed_text_block_still_streams(self):
        from alpha.llm_anthropic import _apply_anthropic_event
        st = self._state()
        _apply_anthropic_event(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "text"}}, st
        )
        out = _apply_anthropic_event(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hi"}}, st
        )
        assert out == "hi"


# ── #D109: retry on retryable HTTP status (429/5xx/529) ──

class _FakeResp:
    def __init__(self, status, *, headers=None, lines=None):
        self.status_code = status
        self.headers = headers or {}
        self._lines = lines or []

    async def aread(self):
        return b'{"error": "rate limited"}'

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def stream(self, *a, **k):
        resp = self._responses[self.calls]
        self.calls += 1
        return _FakeStreamCtx(resp)


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds(monkeypatch):
    import alpha.llm_anthropic as mod

    success_lines = [
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}',
        'data: {"type":"message_stop"}',
    ]
    client = _FakeClient([
        _FakeResp(429, headers={"retry-after": "0"}),
        _FakeResp(200, lines=success_lines),
    ])

    async def fake_get_client():
        return client

    async def fake_acquire(name):
        return None

    async def fake_sleep(secs):
        return None

    monkeypatch.setattr(mod, "_get_client", fake_get_client)
    monkeypatch.setattr(mod, "_rate_limit_acquire", fake_acquire)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

    events = [
        e async for e in mod.stream_anthropic(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], temperature=0.0,
            base_url="https://x/v1", api_key="k", model="m",
        )
    ]
    # Retried once (2 stream calls) and the final event reflects success.
    assert client.calls == 2
    final = events[-1]
    assert final["type"] == "final"
    assert final["error"] is None
    assert final["content"] == "ok"


@pytest.mark.asyncio
async def test_no_retry_on_400(monkeypatch):
    import alpha.llm_anthropic as mod

    client = _FakeClient([_FakeResp(400)])

    async def fake_get_client():
        return client

    async def fake_acquire(name):
        return None

    monkeypatch.setattr(mod, "_get_client", fake_get_client)
    monkeypatch.setattr(mod, "_rate_limit_acquire", fake_acquire)

    events = [
        e async for e in mod.stream_anthropic(
            messages=[{"role": "user", "content": "hi"}],
            tools=[], temperature=0.0,
            base_url="https://x/v1", api_key="k", model="m",
        )
    ]
    # 400 is not retryable — single attempt, error surfaced.
    assert client.calls == 1
    assert events[-1]["error"].startswith("HTTP 400")


class TestPromptCachingTier1:
    """SECURITY_TIER0 follow-up (Tier 1): ephemeral prompt caching on the
    large stable prefixes re-sent every agentic turn."""

    def _build(self, messages, tools=None):
        from alpha.llm_anthropic import _build_anthropic_request
        return _build_anthropic_request(messages, tools or [], 0.0, "claude-sonnet-4-6", "k")

    def test_system_block_is_cached(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_CACHE", "1")
        _, payload = self._build([
            {"role": "system", "content": "You are Alpha."},
            {"role": "user", "content": "hi"},
        ])
        assert isinstance(payload["system"], list)
        assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_last_tool_is_cached(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_CACHE", "1")
        tools = [
            {"type": "function", "function": {"name": "a", "description": "", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "", "parameters": {}}},
        ]
        _, payload = self._build([{"role": "user", "content": "hi"}], tools)
        assert payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in payload["tools"][0]

    def test_last_message_string_promoted_and_cached(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_CACHE", "1")
        _, payload = self._build([{"role": "user", "content": "hello"}])
        last = payload["messages"][-1]["content"]
        assert isinstance(last, list)
        assert last[-1]["cache_control"] == {"type": "ephemeral"}

    def test_kill_switch_disables_all_caching(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_CACHE", "0")
        tools = [{"type": "function", "function": {"name": "a", "description": "", "parameters": {}}}]
        _, payload = self._build([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ], tools)
        assert payload["system"] == "sys"  # plain string, not a block list
        assert "cache_control" not in payload["tools"][0]
        assert payload["messages"][-1]["content"] == "hi"  # not promoted


class TestMaxTokensConfigurable:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("ALPHA_MAX_TOKENS", raising=False)
        from alpha.llm_anthropic import _max_tokens, _DEFAULT_MAX_TOKENS
        assert _max_tokens() == _DEFAULT_MAX_TOKENS

    def test_override(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_TOKENS", "32000")
        from alpha.llm_anthropic import _max_tokens
        assert _max_tokens() == 32000

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_TOKENS", "abc")
        from alpha.llm_anthropic import _max_tokens, _DEFAULT_MAX_TOKENS
        assert _max_tokens() == _DEFAULT_MAX_TOKENS

    def test_payload_uses_it(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_TOKENS", "12345")
        from alpha.llm_anthropic import _build_anthropic_request
        _, payload = _build_anthropic_request([{"role": "user", "content": "hi"}], [], 0.0, "m", "k")
        assert payload["max_tokens"] == 12345


# ── Extended thinking ──


class TestThinkingEnablement:
    def test_auto_on_for_supported_models(self, monkeypatch):
        monkeypatch.delenv("ALPHA_ANTHROPIC_THINKING", raising=False)
        from alpha.llm_anthropic import _thinking_enabled
        for model in (
            "claude-3-7-sonnet-20250219",
            "claude-sonnet-4-6",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
            "claude-fable-5",
        ):
            assert _thinking_enabled(model), model

    def test_auto_off_for_unsupported_models(self, monkeypatch):
        monkeypatch.delenv("ALPHA_ANTHROPIC_THINKING", raising=False)
        from alpha.llm_anthropic import _thinking_enabled
        for model in ("claude-3-5-sonnet-20241022", "claude-3-haiku", "gpt-4o", "m"):
            assert not _thinking_enabled(model), model

    def test_env_off_forces_off(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_THINKING", "off")
        from alpha.llm_anthropic import _thinking_enabled
        assert not _thinking_enabled("claude-sonnet-4-6")

    def test_env_on_forces_on(self, monkeypatch):
        monkeypatch.setenv("ALPHA_ANTHROPIC_THINKING", "on")
        from alpha.llm_anthropic import _thinking_enabled
        assert _thinking_enabled("qualquer-modelo")

    def test_budget_default_and_clamp(self, monkeypatch):
        from alpha.llm_anthropic import (
            _THINKING_DEFAULT_BUDGET,
            _THINKING_MIN_BUDGET,
            _thinking_budget,
        )
        monkeypatch.delenv("ALPHA_THINKING_BUDGET", raising=False)
        assert _thinking_budget() == _THINKING_DEFAULT_BUDGET
        monkeypatch.setenv("ALPHA_THINKING_BUDGET", "100")
        assert _thinking_budget() == _THINKING_MIN_BUDGET
        monkeypatch.setenv("ALPHA_THINKING_BUDGET", "16000")
        assert _thinking_budget() == 16000
        monkeypatch.setenv("ALPHA_THINKING_BUDGET", "abc")
        assert _thinking_budget() == _THINKING_DEFAULT_BUDGET


class TestThinkingPayload:
    def _build(self, model, temperature=0.5):
        from alpha.llm_anthropic import _build_anthropic_request
        return _build_anthropic_request(
            [{"role": "user", "content": "hi"}], [], temperature, model, "k"
        )

    def test_thinking_param_and_temperature(self, monkeypatch):
        monkeypatch.delenv("ALPHA_ANTHROPIC_THINKING", raising=False)
        monkeypatch.delenv("ALPHA_THINKING_BUDGET", raising=False)
        monkeypatch.delenv("ALPHA_MAX_TOKENS", raising=False)
        from alpha.llm_anthropic import _DEFAULT_MAX_TOKENS, _THINKING_DEFAULT_BUDGET
        _, payload = self._build("claude-sonnet-4-6")
        assert payload["thinking"] == {
            "type": "enabled",
            "budget_tokens": _THINKING_DEFAULT_BUDGET,
        }
        # Budget é adicional ao cap de saída; temperature deve virar 1.
        assert payload["max_tokens"] == _DEFAULT_MAX_TOKENS + _THINKING_DEFAULT_BUDGET
        assert payload["temperature"] == 1

    def test_no_thinking_for_unsupported_model(self, monkeypatch):
        monkeypatch.delenv("ALPHA_ANTHROPIC_THINKING", raising=False)
        _, payload = self._build("claude-3-5-sonnet-20241022", temperature=0.3)
        assert "thinking" not in payload
        assert payload["temperature"] == 0.3


class TestThinkingStreaming:
    def _state(self):
        from alpha.llm_anthropic import _AnthropicStreamState
        return _AnthropicStreamState()

    def test_thinking_blocks_accumulate_and_collect(self):
        from alpha.llm_anthropic import _apply_anthropic_event, _collect_thinking_blocks
        state = self._state()
        _apply_anthropic_event(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking"}},
            state,
        )
        out = _apply_anthropic_event(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "pensando... "}},
            state,
        )
        assert out is None  # thinking não vaza como content_token
        _apply_anthropic_event(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "mais"}},
            state,
        )
        _apply_anthropic_event(
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "sig123"}},
            state,
        )
        blocks = _collect_thinking_blocks(state.blocks)
        assert blocks == [
            {"type": "thinking", "thinking": "pensando... mais", "signature": "sig123"}
        ]

    def test_redacted_thinking_captured(self):
        from alpha.llm_anthropic import _apply_anthropic_event, _collect_thinking_blocks
        state = self._state()
        _apply_anthropic_event(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "redacted_thinking", "data": "enc"}},
            state,
        )
        assert _collect_thinking_blocks(state.blocks) == [
            {"type": "redacted_thinking", "data": "enc"}
        ]

    def test_empty_thinking_not_collected(self):
        from alpha.llm_anthropic import _apply_anthropic_event, _collect_thinking_blocks
        state = self._state()
        _apply_anthropic_event(
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking"}},
            state,
        )
        assert _collect_thinking_blocks(state.blocks) == []


class TestThinkingReplay:
    def _assistant_msg(self):
        return {
            "role": "assistant",
            "content": "vou ler o arquivo",
            "tool_calls": [
                {
                    "id": "tc1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "x"}'},
                }
            ],
            "thinking_blocks": [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"}
            ],
        }

    def test_replayed_first_when_enabled(self):
        _, converted = _convert_messages([self._assistant_msg()], include_thinking=True)
        blocks = converted[0]["content"]
        assert blocks[0] == {"type": "thinking", "thinking": "hmm", "signature": "sig"}
        assert blocks[1]["type"] == "text"
        assert blocks[2]["type"] == "tool_use"

    def test_dropped_when_disabled(self):
        _, converted = _convert_messages([self._assistant_msg()], include_thinking=False)
        types = [b["type"] for b in converted[0]["content"]]
        assert "thinking" not in types

    def test_malformed_blocks_skipped(self):
        msg = self._assistant_msg()
        msg["thinking_blocks"] = ["texto solto", {"type": "outro"}, None]
        _, converted = _convert_messages([msg], include_thinking=True)
        types = [b["type"] for b in converted[0]["content"]]
        assert types == ["text", "tool_use"]


class TestBuildAssistantToolMessageThinking:
    def test_thinking_blocks_attached(self):
        from alpha.executor import build_assistant_tool_message
        tb = [{"type": "thinking", "thinking": "t", "signature": "s"}]
        msg = build_assistant_tool_message(
            "oi", [{"id": "1", "name": "x", "arguments": "{}"}], None, tb
        )
        assert msg["thinking_blocks"] == tb

    def test_omitted_when_none(self):
        from alpha.executor import build_assistant_tool_message
        msg = build_assistant_tool_message(
            "oi", [{"id": "1", "name": "x", "arguments": "{}"}]
        )
        assert "thinking_blocks" not in msg


class TestConvertToolsCache:
    """AUDIT_V2.3 #400: memoização por identidade com referência forte."""

    def test_same_list_returns_cached_conversion(self):
        import alpha.llm_anthropic as mod

        tools = [{"type": "function", "function": {
            "name": "t1", "description": "d", "parameters": {"type": "object"}}}]
        a = mod._convert_tools(tools)
        b = mod._convert_tools(tools)
        assert a is b

    def test_different_list_reconverts(self):
        import alpha.llm_anthropic as mod

        t1 = [{"type": "function", "function": {"name": "a", "parameters": {}}}]
        t2 = [{"type": "function", "function": {"name": "b", "parameters": {}}}]
        out1 = mod._convert_tools(t1)
        out2 = mod._convert_tools(t2)
        assert out1[0]["name"] == "a"
        assert out2[0]["name"] == "b"

    def test_cache_control_does_not_mutate_cached_schema(self, monkeypatch):
        """O breakpoint de cache vai numa cópia rasa — reusar o cache não
        pode acumular cache_control no dict memoizado."""
        import alpha.llm_anthropic as mod

        monkeypatch.delenv("ALPHA_ANTHROPIC_CACHE", raising=False)
        tools = [{"type": "function", "function": {
            "name": "t1", "description": "d", "parameters": {"type": "object"}}}]
        msgs = [{"role": "user", "content": "oi"}]
        _h, payload = mod._build_anthropic_request(msgs, tools, 0.5, "m", "k")
        assert "cache_control" in payload["tools"][-1]
        cached = mod._convert_tools(tools)
        assert "cache_control" not in cached[-1]


class TestImageSourceCache:
    """AUDIT_V2.3 #402: bloco de imagem convertido é cacheado no próprio
    bloco OpenAI (mensagens de visão são imutáveis)."""

    def test_second_conversion_reuses_source(self):
        import alpha.llm_anthropic as mod

        block = {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64,QUJD"}}
        content = [block]
        out1 = mod._convert_user_content(content)
        assert block.get("_anthropic_source") is not None
        out2 = mod._convert_user_content(content)
        assert out2[0]["source"] is out1[0]["source"]
        assert out1[0]["source"]["data"] == "QUJD"
        assert out1[0]["source"]["media_type"] == "image/png"
