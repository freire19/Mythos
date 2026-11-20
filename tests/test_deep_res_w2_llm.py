"""Regressao DEEP_RESILIENCE wave 2 — alpha/llm.py.

- #D806: linha SSE `data:` com JSON valido nao-objeto (null / "ping" / 0 / [])
  nao pode derrubar o stream — guarda `isinstance(data, dict)` apos json.loads.
- #D817: chaves de `index` mistas (str+int) no acumulador de tool-calls nao
  podem fazer `sorted()` levantar TypeError — coercao para int na ingestao.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from alpha.llm import _accumulate_tool_call_delta


class _FakeStreamResponse:
    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""


class _FakeAsyncClient:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response
        self.is_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def aclose(self):
        self.is_closed = True

    @asynccontextmanager
    async def stream(self, *_args, **_kwargs):
        yield self._response


def _sse(payload) -> str:
    return f"data: {json.dumps(payload)}"


@pytest.fixture
def _reset_shared_llm_client():
    from alpha import llm

    llm._shared_llm_client._client = None
    llm._shared_llm_client._loop = None
    yield
    llm._shared_llm_client._client = None
    llm._shared_llm_client._loop = None


@pytest.mark.asyncio
class TestNonDictSSEData:
    """#D806 — heartbeats/keepalives com JSON nao-dict sao ignorados."""

    async def test_stream_survives_non_dict_data_lines(
        self, monkeypatch, _reset_shared_llm_client
    ):
        from alpha import llm

        monkeypatch.setattr(
            llm,
            "get_provider_config",
            lambda _p: {
                "base_url": "http://x",
                "api_key": "k",
                "model": "test-model",
                "supports_tools": True,
                "api_format": "openai",
                "low_temperature": False,
            },
        )

        chunks = [
            _sse(None),
            _sse("ping"),
            _sse(0),
            _sse([1, 2, 3]),
            _sse({"choices": [{"delta": {"content": "Hello"}}]}),
            _sse(None),
            _sse({"choices": [{"delta": {"content": " world"}}]}),
            "data: [DONE]",
        ]
        fake_resp = _FakeStreamResponse(chunks)
        monkeypatch.setattr(
            llm.httpx, "AsyncClient", lambda **_: _FakeAsyncClient(fake_resp)
        )

        events = []
        async for ev in llm.stream_chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider="deepseek",
        ):
            events.append(ev)

        finals = [e for e in events if e["type"] == "final"]
        assert len(finals) == 1
        assert finals[0]["error"] is None
        assert finals[0]["content"] == "Hello world"


class TestMixedIndexKeys:
    """#D817 — index str e int misturados nao podem quebrar sorted()."""

    def test_str_index_coerced_to_int(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": "0", "id": "c1", "function": {"name": "a", "arguments": "{}"}}
        )
        assert list(acc.keys()) == [0]

    def test_mixed_str_and_missing_index_sortable(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": "0", "id": "c1", "function": {"name": "a", "arguments": "{}"}}
        )
        _accumulate_tool_call_delta(
            acc, {"id": "c2", "function": {"name": "b", "arguments": "{}"}}
        )
        _accumulate_tool_call_delta(
            acc, {"index": 2, "id": "c3", "function": {"name": "c", "arguments": "{}"}}
        )
        ordered = [tc["id"] for _, tc in sorted(acc.items())]
        assert ordered == ["c1", "c2", "c3"]

    def test_str_delta_continues_same_entry_as_int(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": 0, "id": "c1", "function": {"name": "a", "arguments": '{"x"'}}
        )
        _accumulate_tool_call_delta(
            acc, {"index": "0", "function": {"arguments": ": 1}"}}
        )
        assert len(acc) == 1
        assert acc[0]["arguments"] == '{"x": 1}'

    def test_unparseable_index_falls_back_to_next_slot(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": "abc", "id": "c1", "function": {"name": "a", "arguments": "{}"}}
        )
        assert list(acc.keys()) == [0]
