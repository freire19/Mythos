"""
Session-level cost and token tracking (Plano-Upgrade-v3 H1 #4 — `/cost`).

`record_usage(provider, model, usage)` is called by the agent loop on every
`final` event from `stream_chat_with_tools`. `session_summary()` formats the
running total for the `/cost` slash command.

Pricing is per 1M tokens, USD, prompt vs completion. Unknown models fall
back to (0.0, 0.0) — we still report tokens, just without a $ figure.
Cache-hit discounts and tiered pricing are not modeled — this is a ballpark,
not an invoice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Pricing in USD per 1,000,000 tokens — (prompt_in, completion_out).
# Last updated 2025-12. Source: provider docs. Keep keys lowercased so a
# loose match (substring) tolerates suffix variants like
# "gemini-3.1-pro-preview-customtools".
_PRICING: dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-chat":       (0.14, 0.28),
    "deepseek-reasoner":   (0.55, 2.19),
    "deepseek-v3":         (0.14, 0.28),
    # OpenAI
    "gpt-4o":              (2.50, 10.00),
    "gpt-4o-mini":         (0.15, 0.60),
    "gpt-4.1":             (2.00, 8.00),
    "gpt-4.1-mini":        (0.40, 1.60),
    "gpt-4.1-nano":        (0.10, 0.40),
    "o1":                  (15.00, 60.00),
    "o1-mini":             (3.00, 12.00),
    "o3-mini":             (1.10, 4.40),
    # Anthropic
    "claude-opus-4":       (15.00, 75.00),
    "claude-opus-4-7":     (15.00, 75.00),
    "claude-sonnet-4":     (3.00, 15.00),
    "claude-sonnet-4-6":   (3.00, 15.00),
    "claude-haiku-4":      (0.80, 4.00),
    "claude-haiku-4-5":    (0.80, 4.00),
    # Google
    "gemini-2.5-pro":      (1.25, 10.00),
    "gemini-2.5-flash":    (0.30, 2.50),
    "gemini-3.1-pro":      (1.25, 10.00),
    "gemini-3.1-flash":    (0.30, 2.50),
    # Grok
    "grok-3":              (3.00, 15.00),
    "grok-3-mini":         (0.30, 0.50),
    "grok-4":              (3.00, 15.00),
}


def _price_for(model: str) -> tuple[float, float]:
    """Best-effort price lookup. Returns (0.0, 0.0) if model is unknown.

    Matches by substring so `gemini-3.1-pro-preview-customtools` resolves to
    `gemini-3.1-pro`. Longest-key-first to prefer specific over generic
    (`claude-sonnet-4-6` over `claude-sonnet-4`)."""
    if not model:
        return (0.0, 0.0)
    m = model.lower()
    for key in sorted(_PRICING, key=len, reverse=True):
        if key in m:
            return _PRICING[key]
    return (0.0, 0.0)


@dataclass
class _Entry:
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def cost_usd(self) -> float:
        p_in, p_out = _price_for(self.model)
        return (self.tokens_in * p_in + self.tokens_out * p_out) / 1_000_000


@dataclass
class _SessionTotals:
    entries: dict[tuple[str, str], _Entry] = field(default_factory=dict)
    call_count: int = 0

    def add(self, provider: str, model: str, t_in: int, t_out: int) -> None:
        key = (provider, model)
        e = self.entries.get(key)
        if e is None:
            e = _Entry(provider=provider, model=model)
            self.entries[key] = e
        e.tokens_in += int(t_in)
        e.tokens_out += int(t_out)
        self.call_count += 1

    def reset(self) -> None:
        self.entries.clear()
        self.call_count = 0

    @property
    def total_tokens_in(self) -> int:
        return sum(e.tokens_in for e in self.entries.values())

    @property
    def total_tokens_out(self) -> int:
        return sum(e.tokens_out for e in self.entries.values())

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries.values())


_session = _SessionTotals()


def record_usage(provider: str, model: str, usage: dict | None) -> None:
    """Record one LLM call. `usage` is the dict from the provider's response;
    no-op when None or malformed (some providers/models omit usage)."""
    if not isinstance(usage, dict):
        return
    # OpenAI-style: prompt_tokens / completion_tokens
    # Anthropic-style: input_tokens / output_tokens
    t_in = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or 0
    )
    t_out = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or 0
    )
    if not (t_in or t_out):
        return
    _session.add(provider, model, t_in, t_out)
    logger.debug(
        "cost.record_usage provider=%s model=%s in=%d out=%d total_usd=%.4f",
        provider, model, t_in, t_out, _session.total_cost_usd,
    )


def reset_session() -> None:
    """Reset the running totals — called on /clear."""
    _session.reset()


def session_summary() -> dict:
    """Return a structured snapshot for /cost and /stats."""
    return {
        "calls": _session.call_count,
        "tokens_in": _session.total_tokens_in,
        "tokens_out": _session.total_tokens_out,
        "cost_usd": _session.total_cost_usd,
        "by_model": [
            {
                "provider": e.provider,
                "model": e.model,
                "tokens_in": e.tokens_in,
                "tokens_out": e.tokens_out,
                "cost_usd": e.cost_usd,
            }
            for e in sorted(
                _session.entries.values(),
                key=lambda x: x.cost_usd,
                reverse=True,
            )
        ],
    }


def format_session_line() -> str:
    """One-line summary suitable for status bars / banners."""
    s = session_summary()
    if s["calls"] == 0:
        return "no LLM calls this session"
    return (
        f"{s['calls']} call(s) — "
        f"{s['tokens_in']:,} in / {s['tokens_out']:,} out — "
        f"${s['cost_usd']:.4f}"
    )
