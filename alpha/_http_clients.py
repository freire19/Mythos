"""Shared HTTP client pools for LLM providers.

Loop-aware singleton httpx.AsyncClient pools that survive across
``asyncio.run()`` boundaries (new loop → new client).  Used by
``llm.py`` (OpenAI path) and ``llm_anthropic.py`` (Anthropic path)
to avoid TLS-handshake-per-call overhead.

Also provides a global rate limiter (``get_llm_rate_limiter``) so
parallel sub-agents don't exhaust provider rate limits.
"""

import asyncio
import logging

import httpx

from .config import LLM_TIMEOUT

logger = logging.getLogger(__name__)

# ── Shared LLM client ──

_shared_llm_client: httpx.AsyncClient | None = None
_llm_client_loop: object | None = None
_llm_client_lock = asyncio.Lock()


async def get_llm_client(timeout: float | None = None) -> httpx.AsyncClient:
    """Return a loop-aware shared httpx.AsyncClient for LLM API calls.

    The client is created once per event loop and reused across calls.
    When ``asyncio.run()`` creates a new loop, a new client is built.

    Args:
        timeout: Per-session timeout override.  Defaults to
                 ``LLM_TIMEOUT`` on first creation.  Callers that
                 need a different timeout should pass it on every
                 call (the client is NOT recreated if timeout
                 changes — pass ``timeout=`` to the per-request
                 ``client.stream()`` for per-request overrides).
    """
    global _shared_llm_client, _llm_client_loop
    loop = asyncio.get_running_loop()
    effective_timeout = timeout if timeout is not None else LLM_TIMEOUT

    # Fast path: healthy client, same loop
    if (
        _shared_llm_client is not None
        and not _shared_llm_client.is_closed
        and _llm_client_loop is loop
    ):
        return _shared_llm_client

    async with _llm_client_lock:
        # Double-check after acquiring lock
        if (
            _shared_llm_client is not None
            and not _shared_llm_client.is_closed
            and _llm_client_loop is loop
        ):
            return _shared_llm_client
        # Close stale client from previous loop
        if _shared_llm_client is not None and not _shared_llm_client.is_closed:
            try:
                await _shared_llm_client.aclose()
            except Exception:
                pass
        _shared_llm_client = httpx.AsyncClient(
            timeout=httpx.Timeout(effective_timeout, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
        )
        _llm_client_loop = loop
    return _shared_llm_client


# ── Global LLM rate limiter (per provider) ──

# Conservative defaults so parallel sub-agents don't exhaust free-tier
# rate limits.  Paid tiers can bump these via env or direct assignment.
_DEFAULT_PROVIDER_CONCURRENCY: dict[str, int] = {
    "deepseek": 1,   # free tier: ~1 req/s sustained
    "openai": 3,     # tier 1: 3 RPM
    "anthropic": 2,  # tier 1: 2 RPM
    "grok": 2,
    "ollama": 4,     # local — generous
}
_provider_semaphores: dict[str, asyncio.Semaphore] = {}
_provider_sem_lock = asyncio.Lock()
_MAX_SEMAPHORE_ENTRIES = 16  # prevent unbounded growth (#093)


async def get_llm_rate_limiter(provider: str) -> asyncio.Semaphore:
    """Return a per-provider Semaphore limiting concurrent LLM calls.

    Sub-agents acquire this before each LLM stream and release after
    the stream ends, preventing ``delegate_parallel(N)`` from
    hammering a provider past its rate limit.
    """
    if provider in _provider_semaphores:
        return _provider_semaphores[provider]

    async with _provider_sem_lock:
        if provider in _provider_semaphores:
            return _provider_semaphores[provider]
        limit = _DEFAULT_PROVIDER_CONCURRENCY.get(provider, 2)
        if len(_provider_semaphores) >= _MAX_SEMAPHORE_ENTRIES:
            _provider_semaphores.pop(next(iter(_provider_semaphores)))
        _provider_semaphores[provider] = asyncio.Semaphore(limit)
    return _provider_semaphores[provider]
