"""DEEP_MAINTAINABILITY wave 2 — core/docs cluster (#DM071..#DM075).

Refactors preservadores de comportamento:
- #DM071: default de provider unificado em config.DEFAULT_PROVIDER
  (llm.stream_chat_with_tools / agent.run_agent aceitam None; plan_tools
  não re-lê ALPHA_PROVIDER com literal duplicado).
- #DM072: primitivas compartilhadas promovidas a nomes públicos com alias
  de retrocompat (llm, context, cost).
- #DM073: context.MAX_MESSAGES passa a ler config.LIMITS["max_messages"].
- #DM074: cache de fuzzy-path agora é LRU de verdade (hit renova posição).
- #DM075: _RECENT_WINDOW_CAP e _ONE_MEGA_CONTEXT_BUDGET nomeados.
"""

from __future__ import annotations

import inspect

import pytest


# ─── #DM071 — default de provider unificado ─────────────────────────


class TestDefaultProviderUnified:
    def test_stream_chat_with_tools_default_is_none(self):
        from alpha import llm

        sig = inspect.signature(llm.stream_chat_with_tools)
        assert sig.parameters["provider"].default is None

    def test_run_agent_default_is_none(self):
        from alpha import agent

        sig = inspect.signature(agent.run_agent)
        assert sig.parameters["provider"].default is None

    def test_agent_imports_default_provider(self):
        import alpha.agent as agent_mod
        import alpha.config as config_mod

        assert agent_mod.DEFAULT_PROVIDER == config_mod.DEFAULT_PROVIDER

    @pytest.mark.asyncio
    async def test_pre_flight_resolves_model_via_default_provider(self, monkeypatch):
        """Sem `model`, pre_flight resolve via config.DEFAULT_PROVIDER —
        não mais re-lendo ALPHA_PROVIDER com o literal "deepseek"."""
        import alpha.config as config_mod
        from alpha.tools.plan_tools import _pre_flight

        seen: list[str] = []

        def fake_get_provider_config(provider):
            seen.append(provider)
            return {"model": "modelo-do-default"}

        monkeypatch.setattr(config_mod, "DEFAULT_PROVIDER", "provider-sentinela")
        monkeypatch.setattr(
            config_mod, "get_provider_config", fake_get_provider_config
        )

        result = await _pre_flight(
            goal="testar resolução de provider",
            steps=[{"tool": "read_file", "args_preview": "x.py"}],
            confidence="high",
        )
        assert seen == ["provider-sentinela"]
        assert result["model"] == "modelo-do-default"
        assert result["approved"] is True

    @pytest.mark.asyncio
    async def test_pre_flight_explicit_model_skips_provider_lookup(self, monkeypatch):
        import alpha.config as config_mod
        from alpha.tools.plan_tools import _pre_flight

        def boom(provider):  # pragma: no cover — não deve ser chamado
            raise AssertionError("model explícito não deve resolver provider")

        monkeypatch.setattr(config_mod, "get_provider_config", boom)
        result = await _pre_flight(
            goal="g",
            steps=[{"tool": "read_file", "args_preview": "x.py"}],
            confidence="high",
            model="modelo-explicito",
        )
        assert result["model"] == "modelo-explicito"


# ─── #DM072 — aliases públicos com retrocompat ──────────────────────


class TestPublicAliases:
    def test_llm_aliases_are_same_objects(self):
        from alpha import llm

        assert llm.StreamAccumulator is llm._StreamAccumulator
        assert llm.calc_backoff is llm._calc_backoff
        assert llm.parse_retry_after is llm._parse_retry_after

    def test_context_aliases_are_same_objects(self):
        from alpha import context

        assert context.find_compressible_range is context._find_compressible_range
        assert context.hard_truncate is context._hard_truncate

    def test_cost_alias_is_same_object(self):
        from alpha import cost

        assert cost.price_for is cost._price_for

    def test_llm_anthropic_uses_public_names(self):
        import alpha.llm_anthropic as la

        # O adapter deve referenciar os nomes públicos (importados de llm).
        assert hasattr(la, "StreamAccumulator")
        assert hasattr(la, "calc_backoff")
        assert hasattr(la, "parse_retry_after")

    def test_price_for_behavior_preserved(self):
        from alpha.cost import _price_for, price_for

        assert price_for("deepseek-chat") == _price_for("deepseek-chat")
        assert price_for("modelo-desconhecido-xyz") == (0.0, 0.0)


# ─── #DM073 — LIMITS["max_messages"] como fonte única ───────────────


class TestMaxMessagesFromConfig:
    def test_context_reads_config_limit(self):
        from alpha.config import LIMITS
        from alpha.context import MAX_MESSAGES

        assert MAX_MESSAGES == LIMITS["max_messages"]

    def test_value_unchanged(self):
        from alpha.context import MAX_MESSAGES

        assert MAX_MESSAGES == 500  # contrato de comportamento preservado


# ─── #DM074 — cache fuzzy-path agora é LRU real ─────────────────────


class TestFuzzyCacheIsLru:
    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        from alpha.tools import path_helpers

        path_helpers._fuzzy_cache.clear()
        path_helpers._fuzzy_cache_order.clear()
        yield
        path_helpers._fuzzy_cache.clear()
        path_helpers._fuzzy_cache_order.clear()

    def test_hit_renews_position(self, monkeypatch):
        from alpha.tools import path_helpers

        monkeypatch.setattr(path_helpers, "_FUZZY_CACHE_SIZE", 2)
        monkeypatch.setattr(
            path_helpers, "_fuzzy_resolve_uncached", lambda p: None
        )

        path_helpers._fuzzy_resolve("/a")  # cache: [a]
        path_helpers._fuzzy_resolve("/b")  # cache: [a, b]
        path_helpers._fuzzy_resolve("/a")  # HIT — renova /a: [b, a]
        path_helpers._fuzzy_resolve("/c")  # evicta o LRU (/b): [a, c]

        assert "/a" in path_helpers._fuzzy_cache  # FIFO teria evictado /a
        assert "/b" not in path_helpers._fuzzy_cache
        assert "/c" in path_helpers._fuzzy_cache

    def test_hit_still_skips_recompute(self, monkeypatch):
        from alpha.tools import path_helpers

        calls = {"n": 0}

        def counting(p):
            calls["n"] += 1
            return None

        monkeypatch.setattr(path_helpers, "_fuzzy_resolve_uncached", counting)
        path_helpers._fuzzy_resolve("/mesmo/path")
        path_helpers._fuzzy_resolve("/mesmo/path")
        assert calls["n"] == 1

    def test_order_list_has_no_duplicates_after_hits(self, monkeypatch):
        from alpha.tools import path_helpers

        monkeypatch.setattr(
            path_helpers, "_fuzzy_resolve_uncached", lambda p: None
        )
        for _ in range(3):
            path_helpers._fuzzy_resolve("/x")
        assert path_helpers._fuzzy_cache_order.count("/x") == 1


# ─── #DM075 — constantes nomeadas ───────────────────────────────────


class TestNamedConstants:
    def test_recent_window_cap_matches_cycle_window(self):
        import alpha.agent as agent_mod
        from alpha.agent.loop import _CYCLE_WINDOW

        assert agent_mod._RECENT_WINDOW_CAP == _CYCLE_WINDOW * 3

    def test_track_and_detect_loop_truncates_to_cap(self):
        import alpha.agent as agent_mod

        cap = agent_mod._RECENT_WINDOW_CAP
        recent_calls = [f"sig-{i}" for i in range(cap + 10)]
        agent_mod._track_and_detect_loop(
            ["nova-sig"], recent_calls, [], []
        )
        assert len(recent_calls) == cap
        assert recent_calls[-1] == "nova-sig"

    def test_one_mega_budget_constant(self):
        from alpha import context

        assert context._ONE_MEGA_CONTEXT_BUDGET == 900_000
        assert context.PROVIDER_CONTEXT_LIMITS["google"] == 900_000

    def test_get_context_limit_1m_paths_preserved(self, monkeypatch):
        from alpha.context import get_context_limit

        monkeypatch.delenv("ALPHA_CONTEXT_LIMIT", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6[1m]")
        assert get_context_limit("anthropic") == 900_000

        monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
        assert get_context_limit("deepseek") == 900_000
