"""
Provider protocol + registry (Plano-Upgrade-v3 §1.2 / H2 #7).

Before this, `alpha/llm.py:stream_chat_with_tools` dispatched to the
Anthropic adapter via an `if api_format == "anthropic":` branch — every
new format with a different shape (Gemini-native, Bedrock Converse, etc.)
was another `if`/`elif` in the hot path.

Now: implementations register themselves under their `api_format` key
and the dispatcher does a single lookup. Adding a new provider is one
file in `alpha/providers/` plus one `register(name, impl)` call; the
agent loop and llm.py never need to change.

Conformance:
- An implementation is any async generator
  `(messages, tools, temperature, provider) -> AsyncGenerator[dict, None]`
  that yields the standard event shapes (`content_token`, `final`,
  `stream_reset`) the agent loop consumes.
- OpenAI-compat is the default — every `api_format` value that isn't
  registered falls through to the inline OpenAI streaming in llm.py.
  Most providers (DeepSeek, OpenAI, Grok, Gemini's openai-compat layer,
  Ollama) speak OpenAI dialect so they need no entry here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Protocol


class ProviderImpl(Protocol):
    """Async generator signature every registered provider must match."""

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict],
        temperature: float,
        *,
        provider: str = "",
    ) -> AsyncGenerator[dict, None]:
        ...


_REGISTRY: dict[str, ProviderImpl] = {}


def register(format_name: str, impl: ProviderImpl) -> None:
    """Register `impl` as the implementation for `api_format=format_name`.

    Idempotent: re-registering overwrites the previous entry (useful for
    tests that want to swap in a mock)."""
    _REGISTRY[format_name] = impl


def get(format_name: str) -> ProviderImpl | None:
    """Return the registered implementation or None when not registered."""
    return _REGISTRY.get(format_name)


def registered_formats() -> list[str]:
    """For introspection / debug / tests."""
    return sorted(_REGISTRY)
