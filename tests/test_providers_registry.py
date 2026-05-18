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


def test_unregistered_format_falls_through_to_openai_path():
    """Source-level guard: stream_chat_with_tools must contain the
    fall-through comment for unregistered formats. Without this, a
    typo'd api_format would route an unrecognized provider into a
    silent no-op rather than the OpenAI default path."""
    import inspect

    from alpha import llm

    src = inspect.getsource(llm.stream_chat_with_tools)
    # The dispatcher branch keys on api_format != "openai" and looks
    # up the registry; on a miss it falls through to the inline OpenAI
    # streaming code below.
    assert 'api_format != "openai"' in src
    assert "_get_provider_impl(api_format)" in src
    # The comment about fall-through is load-bearing for future devs.
    assert "fall through to OpenAI" in src.lower() or "OpenAI compat" in src
