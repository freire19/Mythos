"""Global LLM rate limiter — token bucket per provider (#D008).

Prevents sub-agents in delegate_parallel from exhausting provider rate limits.
Each provider gets its own bucket; all LLM calls (main agent + sub-agents)
acquire a token before making an HTTP request.

Configurable via env vars:
  ALPHA_RATE_LIMIT_<PROVIDER>=<req_per_minute>,<burst>
  Default: 60 req/min, burst=10 (generous — doesn't affect single-agent use).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from ._loop_lock import LoopAwareLock

logger = logging.getLogger(__name__)


class TokenBucket:
    """Token bucket rate limiter — thread-safe via asyncio.Lock."""

    __slots__ = ("rate", "burst", "tokens", "last_refill", "_lock")

    def __init__(self, rate: float, burst: int):
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        # #D045: loop-aware lock. Buckets are cached module-level and the lock
        # is contended on EVERY delegate_parallel call; a plain asyncio.Lock()
        # binds to the first contending loop and crashes on the next REPL turn
        # (new loop). Rebind lazily per loop.
        self._lock = LoopAwareLock()

    async def acquire(self) -> None:
        """Acquire one token, waiting if necessary.

        AUDIT_V1.10 #020: a coroutine deve RESERVAR seu token atomicamente sob
        o lock antes de dormir, senao duas coroutines consomem o mesmo token
        parcial. AUDIT_V2.1 #003: mas o sleep nao pode ficar DENTRO do lock —
        isso serializa todos os acquirers concorrentes do mesmo provider (cada
        delegate_parallel espera o sleep do anterior). A solucao: reservar o
        slot sob o lock (avancar `last_refill` para o instante em que o token
        fica disponivel) e dormir FORA do lock. A reserva ja torna o consume
        atomico; o sleep apenas aguarda o relogio.
        """
        if self.rate <= 0:
            return
        async with self._lock():
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            # Calculate wait time for 1 token
            wait = (1.0 - self.tokens) / self.rate
            self.tokens = 0.0
            # AUDIT_V2.1 #003: advance last_refill to the instant this waiter's
            # token actually becomes available (now + wait), NOT `now` (set
            # above). Leaving it at `now` made the NEXT acquire() credit the
            # slept interval AGAIN — its `elapsed` would span `wait` and refill
            # a token for time this waiter already consumed → ~2x under-throttle.
            # This reservation is also what makes it safe to sleep outside the
            # lock: the next acquirer computes its wait relative to THIS pushed
            # last_refill, staggering behind us instead of double-consuming.
            self.last_refill = now + wait

        # Sleep OUTSIDE the lock — the token is already reserved above, so
        # concurrent acquirers can enter, reserve their own future slot, and
        # sleep in parallel rather than serializing behind this waiter.
        logger.debug(f"Rate limiter: waiting {wait:.2f}s for token")
        await asyncio.sleep(wait)


# ─── Provider defaults (requests per minute, burst) ───

_DEFAULT_LIMITS: dict[str, tuple[float, int]] = {
    # DeepSeek free tier: very restrictive. Default: 5 req/min, burst=2.
    "deepseek": (5.0, 2),
    # OpenAI free tier: ~3 req/min, burst=2.
    "openai": (3.0, 2),
    # Anthropic: varies. Default: 10 req/min, burst=3.
    "anthropic": (10.0, 3),
    # Grok: generous. Default: 30 req/min, burst=5.
    "grok": (30.0, 5),
    # Ollama (local): no limit needed. Default: 600 req/min, burst=30.
    "ollama": (600.0, 30),
    # Fallback for unknown providers.
    "_default": (60.0, 10),
}

# Global bucket cache: provider_name -> TokenBucket
_buckets: dict[str, TokenBucket] = {}


def _parse_limit(provider: str) -> tuple[float, int]:
    """Parse ALPHA_RATE_LIMIT_<PROVIDER> env var or use default."""
    env_key = f"ALPHA_RATE_LIMIT_{provider.upper()}"
    raw = os.environ.get(env_key, "")
    if raw:
        try:
            parts = raw.split(",")
            rpm = float(parts[0].strip())
            if rpm <= 0:
                raise ValueError(f"rpm deve ser > 0, recebido {rpm}")
            burst = int(parts[1].strip()) if len(parts) > 1 else max(1, int(rpm / 6))
            return (rpm / 60.0, burst)
        except (ValueError, IndexError):
            logger.warning(f"Invalid {env_key}='{raw}', using default")
    return _DEFAULT_LIMITS.get(provider.lower(), _DEFAULT_LIMITS["_default"])


def get_provider_limiter(provider: str) -> TokenBucket:
    """Get or create the TokenBucket for a provider."""
    key = provider.lower()
    if key not in _buckets:
        rate, burst = _parse_limit(key)
        _buckets[key] = TokenBucket(rate=rate, burst=burst)
        logger.info(
            f"Rate limiter for '{key}': {rate * 60:.0f} req/min, burst={burst}"
        )
    return _buckets[key]


async def acquire_llm_token(provider: str) -> None:
    """Acquire a rate-limit token before making an LLM API call."""
    bucket = get_provider_limiter(provider)
    await bucket.acquire()
