"""Thinking renderizado na UI (gap P2 fechado — Plano 2026-11).

Antes, o raciocínio do modelo (extended thinking da Anthropic e
`reasoning_content` da DeepSeek) era acumulado só para round-trip ao
provider e nunca chegava ao usuário. Agora:

- os dois adapters emitem `{"type": "thinking_token", "token": ...}`
  durante o stream, SEM misturar com `content_token` (o `content` final
  continua limpo e o round-trip continua idêntico);
- `agent.run_agent` traduz para o evento público `{"type": "thinking"}`;
- `sdk.run_task` não acumula `thinking` em `TaskResult.events` (mesma
  regra do `token` — é telemetria de stream, não resultado);
- `display.print_thinking` renderiza dim com modo `compact|full|off`
  (`ALPHA_SHOW_THINKING`, default compact);
- `_TurnState` no REPL bufferiza e descarrega o thinking ANTES do trecho
  de resposta que ele precedeu, e nunca o inclui no reply.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from alpha.cli import entry


# ─── adapter Anthropic ───────────────────────────────────────────────


class _Resp:
    def __init__(self, lines):
        self.status_code = 200
        self.headers = {}
        self._lines = lines

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _Ctx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, *a, **k):
        return _Ctx(_Resp(self._lines))


def _anthropic_lines():
    evs = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "preciso ler "}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "o arquivo"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_stop"},
    ]
    return [f"data: {json.dumps(e)}" for e in evs]


@pytest.mark.asyncio
async def test_anthropic_emits_thinking_tokens_separately(monkeypatch):
    import alpha.llm_anthropic as mod

    client = _Client(_anthropic_lines())

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
    thinking = [e["token"] for e in events if e["type"] == "thinking_token"]
    content = [e["token"] for e in events if e["type"] == "content_token"]
    assert "".join(thinking) == "preciso ler o arquivo"
    assert content == ["ok"]
    # thinking vem ANTES do primeiro content_token
    types = [e["type"] for e in events]
    assert types.index("thinking_token") < types.index("content_token")
    final = events[-1]
    assert final["type"] == "final"
    assert final["content"] == "ok"  # thinking nunca vaza pro content
    assert final["thinking_blocks"] == [
        {"type": "thinking", "thinking": "preciso ler o arquivo", "signature": "sig"}
    ]


def test_anthropic_apply_event_queues_thinking_without_returning_it():
    from alpha.llm_anthropic import _AnthropicStreamState, _apply_anthropic_event

    st = _AnthropicStreamState()
    _apply_anthropic_event(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        st,
    )
    out = _apply_anthropic_event(
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        st,
    )
    assert out is None  # contrato str|None de content preservado
    assert st.pending_thinking == ["hmm"]
    # delta vazio não enfileira nada
    _apply_anthropic_event(
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": ""}},
        st,
    )
    assert st.pending_thinking == ["hmm"]


# ─── adapter OpenAI-compat (DeepSeek reasoning_content) ──────────────


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.headers = {}

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeAsyncClient:
    def __init__(self, response):
        self._response = response
        self.is_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def aclose(self):
        self.is_closed = True

    @asynccontextmanager
    async def stream(self, *_a, **_k):
        yield self._response


@pytest.fixture
def _reset_shared_llm_client():
    from alpha import llm

    llm._shared_llm_client._client = None
    llm._shared_llm_client._loop = None
    yield
    llm._shared_llm_client._client = None
    llm._shared_llm_client._loop = None


@pytest.mark.asyncio
async def test_openai_path_emits_thinking_tokens(monkeypatch, _reset_shared_llm_client):
    from alpha import llm

    monkeypatch.setattr(
        llm, "get_provider_config",
        lambda _p: {
            "base_url": "http://x", "api_key": "k", "model": "deepseek-reasoner",
            "supports_tools": True, "api_format": "openai", "low_temperature": False,
        },
    )
    chunks = [
        f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'pensando '}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': 'alto'}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'Oi'}}]})}",
        "data: [DONE]",
    ]
    monkeypatch.setattr(
        llm.httpx, "AsyncClient", lambda **_: _FakeAsyncClient(_FakeStreamResponse(chunks))
    )
    events = [
        ev async for ev in llm.stream_chat_with_tools(
            messages=[{"role": "user", "content": "hi"}], tools=[], provider="deepseek",
        )
    ]
    thinking = "".join(e["token"] for e in events if e["type"] == "thinking_token")
    assert thinking == "pensando alto"
    assert [e["token"] for e in events if e["type"] == "content_token"] == ["Oi"]
    final = events[-1]
    assert final["content"] == "Oi"
    assert final["reasoning_content"] == "pensando alto"  # round-trip intacto


def test_process_chunk_queues_reasoning_side_channel():
    from alpha.llm import _StreamState, _process_chunk

    st = _StreamState()
    out = _process_chunk({"choices": [{"delta": {"reasoning_content": "r1"}}]}, st)
    assert out is None
    assert st.pending_thinking == ["r1"]


# ─── agent loop → evento público `thinking` ─────────────────────────


@pytest.mark.asyncio
async def test_run_agent_translates_thinking_token(monkeypatch):
    import alpha.agent as agent_mod

    async def fake_stream(messages, tools, temperature, **_kw):
        yield {"type": "thinking_token", "token": "vou "}
        yield {"type": "thinking_token", "token": "responder"}
        yield {"type": "content_token", "token": "olá"}
        yield {"type": "final", "content": "olá", "tool_calls": [], "error": None}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    events = [
        e async for e in agent_mod.run_agent(
            messages=[{"role": "user", "content": "oi"}],
            user_message="oi", provider="deepseek",
        )
    ]
    thinking = [e["text"] for e in events if e["type"] == "thinking"]
    assert thinking == ["vou ", "responder"]
    assert [e["text"] for e in events if e["type"] == "token"] == ["olá"]
    done = [e for e in events if e["type"] == "done"]
    assert done and done[0]["reply"] == "olá"
    assert not any(e["type"] == "thinking_token" for e in events)


# ─── SDK: thinking não polui TaskResult.events ───────────────────────


@pytest.mark.asyncio
async def test_sdk_run_task_filters_thinking_events(monkeypatch):
    import alpha.agent as agent_mod
    from alpha import sdk

    async def fake_run_agent(*a, **kw):
        yield {"type": "thinking", "text": "hmm"}
        yield {"type": "token", "text": "x"}
        yield {"type": "done", "reply": "x"}

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
    seen = []
    result = await sdk.run_task(
        "t", provider="deepseek", on_event=seen.append,
        _tools_openai=[], _get_tool_fn=lambda n: None,
    )
    assert result.reply == "x"
    assert result.events == []  # thinking/token são stream, não resultado
    assert [e["type"] for e in seen] == ["thinking", "token", "done"]


# ─── config ──────────────────────────────────────────────────────────


class TestShowThinkingMode:
    def test_default_compact(self, monkeypatch):
        from alpha.config import show_thinking_mode

        monkeypatch.delenv("ALPHA_SHOW_THINKING", raising=False)
        assert show_thinking_mode() == "compact"

    @pytest.mark.parametrize("raw,expected", [
        ("full", "full"), ("OFF", "off"), (" compact ", "compact"),
        ("banana", "compact"),  # inválido → default com warning
    ])
    def test_values(self, monkeypatch, raw, expected):
        from alpha.config import show_thinking_mode

        monkeypatch.setenv("ALPHA_SHOW_THINKING", raw)
        assert show_thinking_mode() == expected


# ─── display.print_thinking ─────────────────────────────────────────


class TestPrintThinking:
    def test_off_prints_nothing(self, capsys):
        from alpha.display import print_thinking

        print_thinking("segredo", "off")
        assert capsys.readouterr().out == ""

    def test_empty_prints_nothing(self, capsys):
        from alpha.display import print_thinking

        print_thinking("   \n  ", "full")
        assert capsys.readouterr().out == ""

    def test_full_prints_every_line_with_header(self, capsys):
        from alpha.display import print_thinking

        text = "\n".join(f"linha {i}" for i in range(10))
        print_thinking(text, "full")
        out = capsys.readouterr().out
        assert "Pensando" in out
        for i in range(10):
            assert f"linha {i}" in out
        assert "linhas" not in out  # sem rodapé de fold

    def test_compact_folds_tail(self, capsys):
        from alpha.display import print_thinking
        from alpha.display.core import THINKING_COMPACT_LINES

        n = THINKING_COMPACT_LINES + 4
        text = "\n".join(f"linha {i}" for i in range(n))
        print_thinking(text, "compact")
        out = capsys.readouterr().out
        assert f"linha {THINKING_COMPACT_LINES - 1}" in out
        assert f"linha {THINKING_COMPACT_LINES}" not in out
        assert "(+4 linhas" in out

    def test_compact_short_text_has_no_fold(self, capsys):
        from alpha.display import print_thinking

        print_thinking("uma\nduas", "compact")
        out = capsys.readouterr().out
        assert "uma" in out and "duas" in out
        assert "linhas" not in out

    def test_long_line_truncated(self, capsys):
        from alpha.display import print_thinking
        from alpha.display.theme import DISPLAY_LINE_TRUNCATE

        print_thinking("x" * (DISPLAY_LINE_TRUNCATE + 50), "full")
        out = capsys.readouterr().out
        assert "x" * (DISPLAY_LINE_TRUNCATE + 50) not in out
        assert "…" in out


# ─── REPL _TurnState ────────────────────────────────────────────────


class _DummyIndicator:
    def __init__(self, *a, **kw):
        self.labels: list[str] = []

    def start(self, label=None):
        self.labels.append(label)

    def stop(self):
        pass

    def pause(self):
        pass

    def add_streamed_text(self, text):
        pass


def _patch(monkeypatch, thinking_calls, mode="compact"):
    import alpha.checkpoints as checkpoints_mod

    monkeypatch.setenv("ALPHA_SHOW_THINKING", mode)
    monkeypatch.setattr(checkpoints_mod, "mark_turn", lambda label: None)
    monkeypatch.setattr(entry, "ThinkingIndicator", _DummyIndicator)
    monkeypatch.setattr(entry, "print_tool_call", lambda *a, **kw: None)
    monkeypatch.setattr(entry, "print_tool_result", lambda *a, **kw: None)
    monkeypatch.setattr(entry, "print_turn_summary", lambda **kw: None)
    monkeypatch.setattr(entry, "print_error", lambda msg: None)
    monkeypatch.setattr(entry, "render_markdown", lambda text: text)
    monkeypatch.setattr(entry, "erase_active_indicator_line", lambda: None)
    monkeypatch.setattr(entry, "live_label_for_tool", lambda name, args: name)
    monkeypatch.setattr(
        entry, "print_thinking",
        lambda text, mode="compact": thinking_calls.append((text, mode)),
    )


async def _run(monkeypatch, events, thinking_calls, mode="compact"):
    import alpha.agent as agent_mod

    _patch(monkeypatch, thinking_calls, mode)

    async def fake_run_agent(*a, **kw):
        for ev in events:
            yield ev

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
    return await entry._run_once([], "tarefa", "deepseek", 0.0, lambda n: None, [])


@pytest.mark.asyncio
async def test_turnstate_flushes_thinking_before_text_and_keeps_reply_clean(
    monkeypatch, capsys
):
    calls: list = []
    events = [
        {"type": "thinking", "text": "primeiro "},
        {"type": "thinking", "text": "raciocínio"},
        {"type": "token", "text": "resposta A"},
        {"type": "tool_call", "id": "c1", "name": "read_file",
         "args": {"path": "x"}, "safety": "safe"},
        {"type": "tool_result", "id": "c1", "name": "read_file", "result": {"ok": True}},
        {"type": "thinking", "text": "segundo"},
        {"type": "token", "text": " resposta B"},
        {"type": "done", "reply": "ignorado"},
    ]
    reply, anomaly = await _run(monkeypatch, events, calls, mode="full")
    assert reply == "resposta A resposta B"  # thinking nunca entra no reply
    assert anomaly is None
    assert calls == [("primeiro raciocínio", "full"), ("segundo", "full")]
    out = capsys.readouterr().out
    assert "resposta A" in out and "resposta B" in out


@pytest.mark.asyncio
async def test_turnstate_off_mode_skips_renderer(monkeypatch):
    calls: list = []
    events = [
        {"type": "thinking", "text": "oculto"},
        {"type": "token", "text": "visível"},
        {"type": "done", "reply": "visível"},
    ]
    reply, _ = await _run(monkeypatch, events, calls, mode="off")
    assert reply == "visível"
    assert calls == []


@pytest.mark.asyncio
async def test_turnstate_relabels_spinner_once_per_segment(monkeypatch):
    calls: list = []
    _patch(monkeypatch, calls)
    st = entry._TurnState()
    st.on_thinking({"text": "a"})
    st.on_thinking({"text": "b"})
    assert st.indicator.labels == ["Pensando"]
    st.flush_text()
    st.on_thinking({"text": "c"})
    assert st.indicator.labels == ["Pensando", "Pensando"]
    st.on_thinking({"text": ""})  # vazio é no-op
    assert st.thinking_parts == ["c"]


@pytest.mark.asyncio
async def test_turnstate_thinking_only_turn_still_flushes(monkeypatch):
    """Thinking sem texto depois (ex.: turno que termina em erro) ainda é
    mostrado — flush_thinking roda mesmo com pending_parts vazio."""
    calls: list = []
    events = [
        {"type": "thinking", "text": "pensei"},
        {"type": "error", "message": "boom"},
    ]
    await _run(monkeypatch, events, calls, mode="compact")
    assert calls == [("pensei", "compact")]
