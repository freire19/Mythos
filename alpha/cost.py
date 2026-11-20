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
from functools import lru_cache

logger = logging.getLogger(__name__)


# Pricing in USD per 1,000,000 tokens — (prompt_in, completion_out).
# Source: provider docs — per-entry vintage varies; see the inline notes
# (ex.: grok fast verificado 2026-08, deepseek v4 provisório). Keep keys
# lowercased so a loose match (substring) tolerates suffix variants like
# "gemini-3.1-pro-preview-customtools".
_PRICING: dict[str, tuple[float, float]] = {
    # DeepSeek
    "deepseek-chat":       (0.14, 0.28),
    "deepseek-reasoner":   (0.55, 2.19),
    "deepseek-v3":         (0.14, 0.28),
    # #200: os modelos v4 (default do provider deepseek) faltavam — sem eles
    # _price_for retornava (0,0) e TODOS os controles de custo (/cost, cap de
    # sessão, pre_flight) ficavam inertes na config de fábrica. Valores
    # provisórios (boundary-match longest-first: `deepseek-v4` cobre variantes).
    "deepseek-v4":         (0.28, 0.42),
    "deepseek-v4-pro":     (0.28, 0.42),
    "deepseek-v4-flash":   (0.14, 0.28),
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
    "gemini-3.5-flash":    (1.50, 9.00),
    "antigravity-preview": (1.25, 10.00),
    # Grok
    "grok-3":              (3.00, 15.00),
    "grok-3-mini":         (0.30, 0.50),
    "grok-4":              (3.00, 15.00),
    # #DL214: chaves exatas para o tier fast (default do provider grok) —
    # sem elas o boundary-match caía em `grok-4` (3.00/15.00) e sobrecobrava.
    # Preço verificado 2026-08 (docs xAI via trackers concordantes):
    # $0.20/M in, $0.50/M out para ambas as variantes 4.1-fast.
    "grok-4-1-fast":               (0.20, 0.50),
    "grok-4-1-fast-reasoning":     (0.20, 0.50),
    "grok-4-1-fast-non-reasoning": (0.20, 0.50),
    # Chaves exatas para os demais default_model de config.py (mesmo preço
    # que o boundary-match já resolvia — registro explícito por auditoria).
    "gemini-3.1-pro-preview-customtools": (1.25, 10.00),
    "antigravity-preview-05-2026":        (1.25, 10.00),
}


# Pre-sorted longest-first so substring lookup prefers `claude-sonnet-4-6`
# over `claude-sonnet-4`. Precomputed at module load — _price_for hits
# this on every cost_usd property access (sort-on-call was O(k log k)
# per lookup, wasted work since _PRICING never mutates at runtime).
# DEEP_PERFORMANCE #DP023: also precompute the boundary suffix `key + "-"`
# so the startswith probe below doesn't allocate a new string per iteration.
_PRICING_KEYS_BY_LEN = sorted(_PRICING, key=len, reverse=True)
_PRICING_KEYS_WITH_DASH = [(key, key + "-") for key in _PRICING_KEYS_BY_LEN]


@lru_cache(maxsize=128)
def _price_for(model: str) -> tuple[float, float]:
    """Best-effort price lookup. Returns (0.0, 0.0) if model is unknown.

    Boundary-matches `<key>-<suffix>` so `gemini-3.1-pro-preview` resolves
    to `gemini-3.1-pro` while `gpt-4.10` does NOT match `gpt-4.1`. Variants
    with distinct pricing (e.g. `gpt-4.1-turbo`) must be registered
    explicitly in `_PRICING` — string-matching cannot disambiguate them.

    DEEP_PERFORMANCE #DP023: memoized with lru_cache — `_PRICING` never mutates
    at runtime and a session sees only a handful of distinct model strings, so
    the per-access recompute in `cost_usd`/`total_cost_usd` collapses to a dict
    hit. Cache key is the raw `model` string (pre-lowercase)."""
    if not model:
        return (0.0, 0.0)
    m = model.lower()
    if m in _PRICING:
        return _PRICING[m]
    for key, key_dash in _PRICING_KEYS_WITH_DASH:
        if m.startswith(key_dash):
            return _PRICING[key]
    return (0.0, 0.0)


# #DM072: alias público — consumido por alpha/preflight/cost_estimate.py; o
# nome underscore fica como retrocompat interna.
price_for = _price_for


# Anthropic prompt-cache multipliers on the input price: a cache READ bills at
# ~0.1x, a cache WRITE (creation) at ~1.25x. Modeling these keeps `/cost` from
# hiding the whole point of caching (cheap re-reads) or overcharging writes.
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


@dataclass
class _Entry:
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    # Anthropic prompt-cache accounting. `tokens_in` stays the UNCACHED input
    # so existing token totals are unchanged; cache tokens are tracked (and
    # priced) separately.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        p_in, p_out = _price_for(self.model)
        return (
            self.tokens_in * p_in
            + self.cache_read_tokens * p_in * _CACHE_READ_MULT
            + self.cache_creation_tokens * p_in * _CACHE_WRITE_MULT
            + self.tokens_out * p_out
        ) / 1_000_000


@dataclass
class _SessionTotals:
    entries: dict[tuple[str, str], _Entry] = field(default_factory=dict)
    call_count: int = 0
    # Input-side breakdown of the LAST recorded call — the size of the
    # context the next follow-up turn will re-send.
    last_call_model: str = ""
    last_call_tokens_in: int = 0
    last_call_cache_read: int = 0
    last_call_cache_creation: int = 0

    def add(
        self,
        provider: str,
        model: str,
        t_in: int,
        t_out: int,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        key = (provider, model)
        e = self.entries.get(key)
        if e is None:
            e = _Entry(provider=provider, model=model)
            self.entries[key] = e
        e.tokens_in += int(t_in)
        e.tokens_out += int(t_out)
        e.cache_read_tokens += int(cache_read)
        e.cache_creation_tokens += int(cache_creation)
        self.call_count += 1
        self.last_call_model = model
        self.last_call_tokens_in = int(t_in)
        self.last_call_cache_read = int(cache_read)
        self.last_call_cache_creation = int(cache_creation)

    def reset(self) -> None:
        self.entries.clear()
        self.call_count = 0
        self.last_call_model = ""
        self.last_call_tokens_in = 0
        self.last_call_cache_read = 0
        self.last_call_cache_creation = 0

    @property
    def total_tokens_in(self) -> int:
        return sum(e.tokens_in for e in self.entries.values())

    @property
    def total_tokens_out(self) -> int:
        return sum(e.tokens_out for e in self.entries.values())

    @property
    def total_cache_read_tokens(self) -> int:
        return sum(e.cache_read_tokens for e in self.entries.values())

    @property
    def total_cache_creation_tokens(self) -> int:
        return sum(e.cache_creation_tokens for e in self.entries.values())

    @property
    def total_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.entries.values())


_session = _SessionTotals()


def _normalize_usage(usage: dict | None) -> tuple[int, int, int, int] | None:
    """Pull (tokens_in, tokens_out, cache_read, cache_creation) from a
    provider usage dict.

    OpenAI-style uses `prompt_tokens` / `completion_tokens`; Anthropic
    uses `input_tokens` / `output_tokens` plus `cache_read_input_tokens` /
    `cache_creation_input_tokens` when prompt caching is active. Anthropic's
    `input_tokens` already EXCLUDES cached tokens, so the three input figures
    are additive, not overlapping. Returns None when the dict is missing,
    malformed, or reports nothing billable at all."""
    if not isinstance(usage, dict):
        return None

    # AUDIT_V2.2 #006: use presence, not truthiness. `a or b or 0` discards a
    # legitimate explicit 0 and can't tell "key absent" (drop, by design) from
    # "key present and zero" (a real, billable-but-cached call → record it).
    def _first_present(*keys: str) -> int | None:
        for k in keys:
            v = usage.get(k)
            if v is not None:
                return int(v)
        return None

    t_in = _first_present("prompt_tokens", "input_tokens")
    t_out = _first_present("completion_tokens", "output_tokens")
    cache_read = _first_present("cache_read_input_tokens") or 0
    cache_creation = _first_present("cache_creation_input_tokens") or 0
    if t_in is None and t_out is None and not cache_read and not cache_creation:
        # Provider omitted usage entirely — stays out of the total (by design).
        return None
    return (t_in or 0), (t_out or 0), cache_read, cache_creation


def record_usage(provider: str, model: str, usage: dict | None) -> None:
    """Record one LLM call. No-op when usage is None or reports nothing
    billable — `/cost` reports only accounted calls so a turn the provider
    didn't account for stays out of the total."""
    normalized = _normalize_usage(usage)
    if normalized is None:
        return
    t_in, t_out, cache_read, cache_creation = normalized
    _session.add(provider, model, t_in, t_out, cache_read, cache_creation)
    logger.debug(
        "cost.record_usage provider=%s model=%s in=%d out=%d cache_r=%d cache_w=%d total_usd=%.4f",
        provider, model, t_in, t_out, cache_read, cache_creation, _session.total_cost_usd,
    )


def reset_session() -> None:
    """Reset the running totals — called on /clear."""
    _session.reset()


def session_cost_usd() -> float:
    """Lightweight cost-only accessor for hot-path callers (agent loop's
    session cap check). Skips the sorted by_model materialization that
    `session_summary` does — at zero entries the overhead is real
    because we call this on every iteration."""
    return _session.total_cost_usd


def last_call_input_cost_usd() -> float:
    """Input-side cost (USD) of the most recent recorded LLM call — i.e.
    the price of re-sending the current accumulated context on one more
    turn. Cache reads/writes keep their multipliers. Returns 0.0 before
    any call is recorded (or when the model is unpriced)."""
    p_in, _ = _price_for(_session.last_call_model)
    return (
        _session.last_call_tokens_in * p_in
        + _session.last_call_cache_read * p_in * _CACHE_READ_MULT
        + _session.last_call_cache_creation * p_in * _CACHE_WRITE_MULT
    ) / 1_000_000


def session_summary() -> dict:
    """Return a structured snapshot for /cost and /stats."""
    return {
        "calls": _session.call_count,
        "tokens_in": _session.total_tokens_in,
        "tokens_out": _session.total_tokens_out,
        "cache_read_tokens": _session.total_cache_read_tokens,
        "cache_creation_tokens": _session.total_cache_creation_tokens,
        "cost_usd": _session.total_cost_usd,
        "by_model": [
            {
                "provider": e.provider,
                "model": e.model,
                "tokens_in": e.tokens_in,
                "tokens_out": e.tokens_out,
                "cache_read_tokens": e.cache_read_tokens,
                "cache_creation_tokens": e.cache_creation_tokens,
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
    cache_note = ""
    cached = s.get("cache_read_tokens", 0)
    if cached:
        cache_note = f" ({cached:,} cached)"
    return (
        f"{s['calls']} call(s) — "
        f"{s['tokens_in']:,} in{cache_note} / {s['tokens_out']:,} out — "
        f"${s['cost_usd']:.4f}"
    )
