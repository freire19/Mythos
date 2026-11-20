"""Sub-agentes retomáveis — `agent_message` / `agent_list` (parity wave
2026-11, item 6).

Contratos:
- um `_run_subagent` concluído registra a sessão (prep + histórico) e
  marca `resumable` no resultado;
- `agent_message(agent_id, msg)` roda o MESMO prep: histórico preservado +
  nova mensagem, mesmas tools/get_tool/workspace, scratch dir recriado;
- desconhecido / ocupado / vazio → erros claros; registry limitado a
  MAX_SESSIONS; `/clear` zera;
- worktree runs não entram no registry;
- `agent_message` é bloqueada dentro de sub-agentes (anti-recursão) e exige
  aprovação; `agent_list` é SAFE.
"""

from __future__ import annotations

import asyncio

import pytest

import alpha.tools.delegate_tools as dt
from alpha import agent_sessions
from alpha.config import FEATURES
from alpha.tools import get_tool, load_all_tools


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    load_all_tools()
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.setitem(FEATURES, "multi_agent_enabled", True)
    monkeypatch.setitem(FEATURES, "delegate_tool_enabled", True)
    agent_sessions.reset()
    yield
    agent_sessions.reset()


def _fake_run_agent(calls: list[dict], reply: str = "resposta"):
    async def fake(messages, user_message, **kw):
        calls.append({"messages": list(messages), "user_message": user_message, **kw})
        # Simula o loop real: anexa a resposta ao histórico in place.
        messages.append({"role": "assistant", "content": reply})
        yield {"type": "token", "text": reply}
        yield {"type": "done", "reply": reply}
    return fake


@pytest.mark.asyncio
async def test_delegate_then_resume_keeps_history(monkeypatch, tmp_path):
    import alpha.agent as agent_mod

    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls, "achei X"))
    first = await dt._run_subagent("investiga X", parent_workspace=str(tmp_path))
    assert first["status"] == "completed" and first["resumable"] is True
    aid = first["agent_id"]
    assert agent_sessions.get(aid) is not None

    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls, "e Y também"))
    second = await dt._agent_message(aid, "e sobre Y?")
    assert second["status"] == "completed"
    assert second["agent_id"] == aid
    assert second["result"] == "e Y também"

    msgs = calls[1]["messages"]
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"].endswith("investiga X")
    assert msgs[2]["content"] == "achei X"
    assert msgs[3]["content"] == "e sobre Y?"
    # mesmas tools / mesmo workspace / mesmo get_tool gated
    assert calls[1]["workspace"] == calls[0]["workspace"]
    assert [t["function"]["name"] for t in calls[1]["tools"]] == \
        [t["function"]["name"] for t in calls[0]["tools"]]
    assert calls[1]["get_tool_fn"]("delegate_task") is None
    assert calls[1]["get_tool_fn"]("agent_message") is None

    s = agent_sessions.get(aid)
    assert s.turns == 2 and s.last_status == "completed"
    assert "e Y também" in s.last_reply_preview


@pytest.mark.asyncio
async def test_resume_strips_control_chars(monkeypatch, tmp_path):
    import alpha.agent as agent_mod

    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))
    first = await dt._run_subagent("t", parent_workspace=str(tmp_path))
    await dt._agent_message(first["agent_id"], "ok\x00\x1b[31m‮")
    assert calls[1]["messages"][-1]["content"] == "ok[31m"


@pytest.mark.asyncio
async def test_unknown_busy_and_empty(monkeypatch, tmp_path):
    import alpha.agent as agent_mod

    r = await dt._agent_message("nope", "oi")
    assert r["ok"] is False and r["category"] == "not_found"

    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))
    first = await dt._run_subagent("t", parent_workspace=str(tmp_path))
    aid = first["agent_id"]
    r = await dt._agent_message(aid, "   ")
    assert r["ok"] is False and r["category"] == "invalid_args"

    # ocupado: segunda chamada concorrente é recusada
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(messages, user_message, **kw):
        started.set()
        await release.wait()
        yield {"type": "done", "reply": "x"}

    monkeypatch.setattr(agent_mod, "run_agent", slow)
    t1 = asyncio.create_task(dt._agent_message(aid, "lento"))
    await started.wait()
    r = await dt._agent_message(aid, "concorrente")
    assert r["ok"] is False and r["category"] == "busy"
    release.set()
    await t1
    assert agent_sessions.get(aid).busy is False


@pytest.mark.asyncio
async def test_agent_list_and_eviction(monkeypatch, tmp_path):
    import alpha.agent as agent_mod

    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent([]))
    monkeypatch.setattr(agent_sessions, "MAX_SESSIONS", 2)
    ids = []
    for i in range(3):
        r = await dt._run_subagent(f"tarefa {i}", parent_workspace=str(tmp_path))
        ids.append(r["agent_id"])
    listing = await dt._agent_list()
    assert listing["count"] == 2
    listed = {a["agent_id"] for a in listing["agents"]}
    assert ids[0] not in listed and ids[1] in listed and ids[2] in listed
    assert listing["agents"][0]["task"] == "tarefa 2"  # mais recente primeiro


@pytest.mark.asyncio
async def test_clear_resets_registry(monkeypatch, tmp_path):
    import alpha.agent as agent_mod
    from alpha.cli.commands import session as session_cmd

    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent([]))
    await dt._run_subagent("t", parent_workspace=str(tmp_path))
    assert agent_sessions.list_sessions()

    monkeypatch.setattr(session_cmd.os, "system", lambda *_: 0)
    monkeypatch.setattr(session_cmd, "print_banner", lambda *a, **k: None)

    class _Ctx:
        history = []
        messages = [{"role": "system", "content": "s"}]
        system_prompt = "s"
        session_id = "x"
        provider = "deepseek"
        cfg = {"model": "m"}

    session_cmd._handle_clear(_Ctx(), ["/clear"])
    assert agent_sessions.list_sessions() == []


def test_blocked_inside_subagents_and_approval(monkeypatch):
    from alpha import approval
    from alpha.approval import needs_approval
    from alpha.tools._delegate_core import DELEGATION_TOOLS

    # Built-in policy only: a personal "always allow agent_message" rule in
    # the developer's own ~/.alpha/settings.json must not decide this test.
    monkeypatch.setattr(approval, "_find_settings_file", lambda: None)
    approval.reset_permission_cache()

    assert "agent_message" in DELEGATION_TOOLS
    assert "agent_message" in dt._resolve_subagent_blocklist(None)
    assert "agent_message" in dt._resolve_subagent_blocklist(lambda n, a: True)
    assert needs_approval("agent_message", {"agent_id": "x", "message": "y"}) is True
    assert get_tool("agent_list").safety.value == "safe"
    assert get_tool("agent_message").safety.value == "destructive"


@pytest.mark.asyncio
async def test_delegated_child_cannot_resume(monkeypatch):
    monkeypatch.setenv("ALPHA_DELEGATED", "1")
    r = await dt._agent_message("x", "y")
    assert r["ok"] is False and r["category"] == "denied"


@pytest.mark.asyncio
async def test_session_messages_are_the_live_history(monkeypatch, tmp_path):
    """O registry guarda a MESMA lista que run_agent mutou — sem cópia
    defasada."""
    import alpha.agent as agent_mod

    calls: list[dict] = []
    monkeypatch.setattr(agent_mod, "run_agent", _fake_run_agent(calls))
    r = await dt._run_subagent("t", parent_workspace=str(tmp_path))
    s = agent_sessions.get(r["agent_id"])
    assert s.prep.messages[-1]["content"] == "resposta"
    assert s.as_dict()["messages"] == 3
