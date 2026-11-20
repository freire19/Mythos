"""Roteamento de modelo por função (parity wave 2026-11, item 8).

- spec `provider:model` em `get_provider_config` sobrepõe o modelo;
- `base_provider`/`split_provider_spec` normalizam chaves por provider
  (rate limit, context limit);
- `route_provider("compress"|"subagent", fallback)` lê
  ALPHA_COMPRESS_PROVIDER / ALPHA_SUBAGENT_PROVIDER, ignora provider
  desconhecido;
- compressão de contexto usa o provider roteado;
- sub-agente: explícito → perfil → roteamento → provider do pai → default.
"""

from __future__ import annotations

import pytest

from alpha import config
from alpha.config import base_provider, get_provider_config, route_provider, split_provider_spec


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    for v in ("ALPHA_COMPRESS_PROVIDER", "ALPHA_SUBAGENT_PROVIDER", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(v, raising=False)


class TestSpec:
    def test_split(self):
        assert split_provider_spec("anthropic") == ("anthropic", None)
        assert split_provider_spec("anthropic:claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")
        assert split_provider_spec(" anthropic : ") == ("anthropic", None)
        assert base_provider("deepseek:deepseek-chat") == "deepseek"

    def test_config_model_override(self):
        plain = get_provider_config("anthropic")
        spec = get_provider_config("anthropic:claude-haiku-4-5")
        assert spec["model"] == "claude-haiku-4-5"
        assert spec["base_url"] == plain["base_url"]
        assert spec["api_format"] == plain["api_format"]
        assert plain["model"] != "claude-haiku-4-5"

    def test_env_model_still_applies_without_override(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-x")
        assert get_provider_config("anthropic")["model"] == "claude-x"
        assert get_provider_config("anthropic:claude-y")["model"] == "claude-y"

    def test_unknown_base_raises(self):
        with pytest.raises(RuntimeError, match="desconhecido"):
            get_provider_config("nope:model")

    def test_context_limit_uses_base(self):
        from alpha.context import get_context_limit

        assert get_context_limit("anthropic:claude-haiku-4-5") == get_context_limit("anthropic")


class TestRouteProvider:
    def test_unset_returns_fallback(self):
        assert route_provider("compress", "deepseek") == "deepseek"
        assert route_provider("subagent", "deepseek") == "deepseek"

    def test_set(self, monkeypatch):
        monkeypatch.setenv("ALPHA_COMPRESS_PROVIDER", "anthropic:claude-haiku-4-5")
        assert route_provider("compress", "deepseek") == "anthropic:claude-haiku-4-5"
        assert route_provider("subagent", "deepseek") == "deepseek"

    def test_unknown_provider_ignored(self, monkeypatch):
        monkeypatch.setenv("ALPHA_SUBAGENT_PROVIDER", "banana:x")
        assert route_provider("subagent", "deepseek") == "deepseek"

    def test_unknown_function(self):
        assert route_provider("whatever", "deepseek") == "deepseek"


@pytest.mark.asyncio
async def test_compression_uses_routed_provider(monkeypatch):
    from alpha import context

    monkeypatch.setenv("ALPHA_COMPRESS_PROVIDER", "anthropic:claude-haiku-4-5")
    seen = {}

    async def fake_stream(messages, tools, temperature, provider=None):
        seen["provider"] = provider
        yield {"type": "final", "content": "- resumo", "tool_calls": [], "error": None}

    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 200}
        for i in range(30)
    ]
    out = await context.compress_context(msgs, provider="deepseek", stream_fn=fake_stream)
    assert seen["provider"] == "anthropic:claude-haiku-4-5"
    assert out is not None


class TestSubagentProviderPrecedence:
    def _prep(self, tmp_path, **kw):
        import alpha.tools.delegate_tools as dt

        prep, err = dt._prepare_subagent_run(
            kw.get("task", "t"), "", "", kw.get("provider", ""), "", kw.get("scope"),
            None, str(tmp_path),
        )
        assert err is None
        return prep

    def test_parent_provider_wins_over_default(self, tmp_path, monkeypatch):
        from alpha.tools._delegate_core import parent_provider

        monkeypatch.setattr(config, "DEFAULT_PROVIDER", "deepseek")
        token = parent_provider.set("anthropic")
        try:
            assert self._prep(tmp_path).provider == "anthropic"
        finally:
            parent_provider.reset(token)

    def test_routing_wins_over_parent(self, tmp_path, monkeypatch):
        from alpha.tools._delegate_core import parent_provider

        monkeypatch.setenv("ALPHA_SUBAGENT_PROVIDER", "deepseek:deepseek-chat")
        token = parent_provider.set("anthropic")
        try:
            assert self._prep(tmp_path).provider == "deepseek:deepseek-chat"
        finally:
            parent_provider.reset(token)

    def test_explicit_and_profile_win_over_routing(self, tmp_path, monkeypatch):
        from alpha.agents import AgentScope

        monkeypatch.setenv("ALPHA_SUBAGENT_PROVIDER", "deepseek:deepseek-chat")
        assert self._prep(tmp_path, provider="anthropic").provider == "anthropic"
        scope = AgentScope(name="p", provider="anthropic")
        assert self._prep(tmp_path, scope=scope).provider == "anthropic"


@pytest.mark.asyncio
async def test_executor_publishes_parent_provider():
    from alpha.executor import execute_tool_calls
    from alpha.tools import ToolDefinition, ToolSafety
    from alpha.tools._delegate_core import parent_provider

    seen = {}

    async def _probe(**kw):
        seen["p"] = parent_provider.get()
        return {"ok": True}

    td = ToolDefinition(
        name="probe_prov", description="", parameters={"type": "object", "properties": {}},
        safety=ToolSafety.SAFE, executor=_probe,
    )
    async for _ in execute_tool_calls(
        [{"id": "x", "name": "probe_prov", "arguments": "{}"}],
        [{"role": "user", "content": "hi"}], needs_approval_fn=lambda n, a: False,
        get_tool_fn=lambda n: td if n == "probe_prov" else None, provider="anthropic",
    ):
        pass
    assert seen["p"] == "anthropic"
    assert parent_provider.get() is None
