"""Regression tests for the DEEP_BUGS wave-3 agent-command fixes.

- #DB06: /agent <nome> must not crash the REPL when the agent profile pins
  a provider without API key (or unknown) — get_provider_config raises
  RuntimeError, which must be caught, printed, and the previous
  provider/active_agent restored.
"""

from __future__ import annotations

import types


from alpha.cli.commands import agent_meta
from alpha.cli.commands._types import DispatchResult


def _make_ctx(provider="deepseek", active_agent=None):
    return types.SimpleNamespace(
        messages=[{"role": "system", "content": "sys"}],
        history=[{"role": "user", "content": "hi"}],
        session_id="sess-1",
        provider=provider,
        temperature=0.2,
        cfg={"model": "old-model"},
        system_prompt="sys",
        tools=[],
        get_tool_fn=None,
        active_agent=active_agent,
    )


def _make_agent(name="perfil", provider="openai", temperature=None, model=None):
    return types.SimpleNamespace(
        name=name, provider=provider, temperature=temperature, model=model
    )


class TestDB06AgentProviderWithoutKey:
    def test_provider_without_key_does_not_raise(self, monkeypatch, capsys):
        # #DB06: previously the RuntimeError from get_provider_config escaped
        # through dispatch() and killed run_repl.
        agent = _make_agent(provider="openai")
        monkeypatch.setattr(agent_meta, "get_agent", lambda name: agent)

        def boom(provider):
            raise RuntimeError("OPENAI_API_KEY nao configurada")

        monkeypatch.setattr(agent_meta, "get_provider_config", boom)

        ctx = _make_ctx(provider="deepseek", active_agent=None)
        result = agent_meta._handle_agent(ctx, ["/agent", "perfil"])

        assert result == DispatchResult.CONTINUE
        out = capsys.readouterr().out
        assert "OPENAI_API_KEY" in out

    def test_reverts_provider_and_agent_on_error(self, monkeypatch, capsys):
        prev_agent = _make_agent(name="antigo", provider="deepseek")
        new_agent = _make_agent(name="perfil", provider="openai")
        monkeypatch.setattr(agent_meta, "get_agent", lambda name: new_agent)
        monkeypatch.setattr(
            agent_meta,
            "get_provider_config",
            lambda p: (_ for _ in ()).throw(RuntimeError("sem key")),
        )

        ctx = _make_ctx(provider="deepseek", active_agent=prev_agent)
        result = agent_meta._handle_agent(ctx, ["/agent", "perfil"])

        assert result == DispatchResult.CONTINUE
        assert ctx.provider == "deepseek"
        assert ctx.active_agent is prev_agent
        # Conversation state untouched on failure.
        assert ctx.session_id == "sess-1"
        assert ctx.history == [{"role": "user", "content": "hi"}]
        assert ctx.cfg == {"model": "old-model"}

    def test_successful_switch_still_works(self, monkeypatch, capsys):
        agent = _make_agent(provider="openai", temperature=0.7, model="gpt-x")
        monkeypatch.setattr(agent_meta, "get_agent", lambda name: agent)
        monkeypatch.setattr(
            agent_meta, "get_provider_config", lambda p: {"model": "default-m"}
        )
        monkeypatch.setattr(agent_meta, "build_system_prompt", lambda a: "novo sys")
        monkeypatch.setattr(
            agent_meta, "get_tools_for_agent", lambda a: (None, [])
        )

        ctx = _make_ctx(provider="deepseek", active_agent=None)
        result = agent_meta._handle_agent(ctx, ["/agent", "perfil"])

        assert result == DispatchResult.CONTINUE
        assert ctx.provider == "openai"
        assert ctx.active_agent is agent
        assert ctx.cfg["model"] == "gpt-x"
        assert ctx.temperature == 0.7
        assert ctx.history == []
        assert ctx.session_id != "sess-1"
