"""Tests for the provider protocol + registry (H2 #7)."""

from __future__ import annotations

import pytest

from alpha import providers


@pytest.fixture(autouse=True)
def _isolate_registry():
    saved = dict(providers._REGISTRY)
    yield
    providers._REGISTRY.clear()
    providers._REGISTRY.update(saved)


def test_anthropic_self_registers_on_import():
    """Importing alpha.llm_anthropic must register the 'anthropic' impl —
    the dispatcher in alpha.llm relies on this side effect."""
    import alpha.llm_anthropic  # noqa: F401
    assert providers.get("anthropic") is not None
    assert "anthropic" in providers.registered_formats()


def test_register_overwrites():
    """Re-registering replaces the previous entry (test mocking pattern)."""
    async def first(*args, **kwargs):
        yield {"type": "final", "content": "a", "tool_calls": [], "error": None}

    async def second(*args, **kwargs):
        yield {"type": "final", "content": "b", "tool_calls": [], "error": None}

    providers.register("test_fmt", first)
    assert providers.get("test_fmt") is first
    providers.register("test_fmt", second)
    assert providers.get("test_fmt") is second


def test_get_missing_returns_none():
    assert providers.get("does_not_exist_format_999") is None


@pytest.mark.asyncio
async def test_dispatcher_uses_registered_provider(monkeypatch):
    """End-to-end: registering a custom 'fake-fmt' impl, then setting a
    provider config with that api_format, should make stream_chat_with_tools
    yield events from the custom impl."""
    from alpha import llm
    from alpha.config import _PROVIDERS

    captured: list[dict] = []

    async def fake_impl(messages, tools, temperature, *, provider=""):
        captured.append({"messages": messages, "provider": provider})
        yield {"type": "content_token", "token": "fake"}
        yield {"type": "final", "content": "fake", "tool_calls": [], "error": None}

    providers.register("fake-fmt", fake_impl)
    monkeypatch.setitem(_PROVIDERS, "fakeprov", {
        "base_url": "http://fake.local",
        "api_key_env": "FAKE_KEY_DOES_NOT_EXIST",
        "model_env": "FAKE_MODEL",
        "default_model": "fake-model",
        "supports_tools": False,
        "api_format": "fake-fmt",
    })
    monkeypatch.setenv("FAKE_KEY_DOES_NOT_EXIST", "x")

    events = [
        e async for e in llm.stream_chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            temperature=0.5,
            provider="fakeprov",
        )
    ]
    assert any(e["type"] == "final" for e in events)
    assert captured and captured[0]["provider"] == "fakeprov"


@pytest.mark.asyncio
async def test_unregistered_format_falls_through_to_openai_path(monkeypatch):
    """Behavioral: a provider whose api_format is NOT in the registry must
    fall through to the inline OpenAI streaming path (not a silent no-op).

    We register nothing for 'mystery-fmt', point a provider at it, and assert
    the request reaches the OpenAI HTTP client (`client.stream`) — proving the
    fall-through. Without it, the dispatcher would return early on the registry
    miss and yield zero events."""
    from alpha import llm
    from alpha.config import _PROVIDERS

    # Make sure the format is genuinely unregistered.
    assert providers.get("mystery-fmt") is None

    streamed: dict = {}

    class _FakeResponse:
        status_code = 200
        headers: dict = {}

        async def aiter_lines(self):
            # Minimal OpenAI-style SSE: one final-ish chunk then DONE.
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

    class _FakeStreamCtx:
        async def __aenter__(self):
            return _FakeResponse()

        async def __aexit__(self, *exc):
            return False

    class _FakeClient:
        def stream(self, method, url, *a, **k):
            streamed["url"] = url
            return _FakeStreamCtx()

    async def fake_get_client():
        return _FakeClient()

    async def noop_rate_limit(provider):
        return None

    monkeypatch.setattr(llm, "_get_shared_llm_client", fake_get_client)
    monkeypatch.setattr(llm, "_rate_limit_acquire", noop_rate_limit)
    monkeypatch.setitem(_PROVIDERS, "mysteryprov", {
        "base_url": "http://mystery.local",
        "api_key_env": "MYSTERY_KEY",
        "model_env": "MYSTERY_MODEL",
        "default_model": "mystery-model",
        "supports_tools": False,
        "api_format": "mystery-fmt",
    })
    monkeypatch.setenv("MYSTERY_KEY", "x")

    events = [
        e async for e in llm.stream_chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            temperature=0.5,
            provider="mysteryprov",
        )
    ]
    # Reached the OpenAI HTTP path (chamou client.stream com a base_url).
    assert streamed.get("url") == "http://mystery.local/chat/completions"
    # E produziu ao menos um evento (nao foi no-op silencioso).
    assert events
