"""Testes de delegate_task com perfis nomeados (agent=) e background=true.

Cobre: anti-recursão via ALPHA_DELEGATED, spawn headless via bg_tasks,
aplicação de perfil (provider/temperature/system_extra/tool filters) no
sub-agente in-process, e schema da tool.
"""

from __future__ import annotations

import pytest

from alpha.agents.scope import AgentScope
from alpha.tools import get_tool, load_all_tools
from alpha.tools.delegate_tools import (
    _delegate_task,
    _run_subagent,
    _spawn_background_subagent,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.delenv("ALPHA_AGENT", raising=False)


class TestAntiRecursion:
    @pytest.mark.anyio
    async def test_delegated_child_cannot_delegate(self, monkeypatch):
        monkeypatch.setenv("ALPHA_DELEGATED", "1")
        result = await _delegate_task("qualquer tarefa")
        assert result["ok"] is False
        assert result["category"] == "denied"
        assert "anti-recursão" in result["error"]

    @pytest.mark.anyio
    async def test_delegated_child_cannot_spawn_background(self, monkeypatch):
        monkeypatch.setenv("ALPHA_DELEGATED", "1")
        result = await _delegate_task("tarefa", background=True)
        assert result["ok"] is False
        assert result["category"] == "denied"


class TestBackgroundSpawn:
    def _patch_bg(self, monkeypatch):
        captured = {}

        def fake_start(command, cwd, env=None):
            captured.update({"command": command, "cwd": cwd, "env": env})
            return {"task_id": 7, "pid": 123, "log": "/tmp/task-7.log"}

        import alpha.bg_tasks as bg
        monkeypatch.setattr(bg, "start", fake_start)
        return captured

    def test_spawns_headless_child(self, monkeypatch):
        captured = self._patch_bg(monkeypatch)

        result = _spawn_background_subagent("pesquise sobre X", context="repo Y")

        assert result["ok"] is True
        assert result["background"] is True
        assert result["task_id"] == 7
        assert "-m main -p" in captured["command"]
        assert "pesquise sobre X" in captured["command"]
        assert "repo Y" in captured["command"]
        assert "--output-format text" in captured["command"]
        assert captured["env"]["ALPHA_DELEGATED"] == "1"
        assert "ALPHA_AGENT" not in captured["env"]

    def test_agent_profile_and_provider_forwarded(self, monkeypatch):
        captured = self._patch_bg(monkeypatch)
        import alpha.agents as agents_mod
        monkeypatch.setattr(
            agents_mod, "get_agent",
            lambda name: AgentScope(name=name) if name == "researcher" else None,
        )

        result = _spawn_background_subagent(
            "tarefa", agent="researcher", provider="anthropic"
        )

        assert result["ok"] is True
        assert captured["env"]["ALPHA_AGENT"] == "researcher"
        assert "--provider anthropic" in captured["command"]
        assert "researcher" in result["message"]

    def test_unknown_agent_fails_before_spawn(self, monkeypatch):
        captured = self._patch_bg(monkeypatch)
        import alpha.agents as agents_mod
        monkeypatch.setattr(agents_mod, "get_agent", lambda name: None)

        result = _spawn_background_subagent("tarefa", agent="fantasma")

        assert result["ok"] is False
        assert result["category"] == "invalid_args"
        assert captured == {}  # bg_tasks.start nunca chamado

    def test_bg_error_propagates(self, monkeypatch):
        import alpha.bg_tasks as bg
        monkeypatch.setattr(
            bg, "start", lambda command, cwd, env=None: {"error": "limite atingido"}
        )
        result = _spawn_background_subagent("tarefa")
        assert result["ok"] is False
        assert result["error"] == "limite atingido"


class TestForegroundProfile:
    def _fake_run_agent(self, captured):
        async def fake(**kwargs):
            captured.update(kwargs)
            yield {"type": "done", "reply": "resultado do sub-agente"}

        return fake

    @pytest.mark.anyio
    async def test_profile_applied(self, monkeypatch, tmp_path):
        load_all_tools()
        captured: dict = {}
        import alpha.agent as agent_mod
        import alpha.agents as agents_mod

        scope = AgentScope(
            name="researcher",
            temperature=0.1,
            system_prompt_extra="FOCO EM PESQUISA PROFUNDA",
            tools_allow=["read_file", "search_files"],
        )
        monkeypatch.setattr(
            agents_mod, "get_agent",
            lambda name: scope if name == "researcher" else None,
        )
        monkeypatch.setattr(agent_mod, "run_agent", self._fake_run_agent(captured))

        result = await _run_subagent(
            "investigue o módulo X",
            agent="researcher",
            parent_workspace=str(tmp_path),
        )

        assert result["status"] == "completed"
        assert result["result"] == "resultado do sub-agente"
        assert captured["temperature"] == 0.1
        assert "FOCO EM PESQUISA PROFUNDA" in captured["messages"][0]["content"]
        offered = {t["function"]["name"] for t in captured["tools"]}
        assert offered == {"read_file", "search_files"}
        # get_tool_fn respeita o filtro do perfil além da blocklist
        assert captured["get_tool_fn"]("read_file") is not None
        assert captured["get_tool_fn"]("write_file") is None
        assert captured["get_tool_fn"]("glob_files") is None

    @pytest.mark.anyio
    async def test_unknown_profile_errors(self, monkeypatch, tmp_path):
        import alpha.agents as agents_mod
        monkeypatch.setattr(agents_mod, "get_agent", lambda name: None)

        result = await _run_subagent(
            "tarefa", agent="fantasma", parent_workspace=str(tmp_path)
        )
        assert result["ok"] is False
        assert result["category"] == "invalid_args"

    @pytest.mark.anyio
    async def test_profile_filtering_everything_errors(self, monkeypatch, tmp_path):
        load_all_tools()
        import alpha.agents as agents_mod
        scope = AgentScope(name="vazio", tools_allow=["tool_inexistente"])
        monkeypatch.setattr(agents_mod, "get_agent", lambda name: scope)

        result = await _run_subagent(
            "tarefa", agent="vazio", parent_workspace=str(tmp_path)
        )
        assert result["ok"] is False
        assert "filtra todas as tools" in result["error"]

    @pytest.mark.anyio
    async def test_no_profile_keeps_default_temperature(self, monkeypatch, tmp_path):
        load_all_tools()
        captured: dict = {}
        import alpha.agent as agent_mod
        monkeypatch.setattr(agent_mod, "run_agent", self._fake_run_agent(captured))

        result = await _run_subagent("tarefa", parent_workspace=str(tmp_path))
        assert result["status"] == "completed"
        assert captured["temperature"] == 0.3


class TestSchema:
    def test_delegate_task_has_new_params(self):
        load_all_tools()
        tool = get_tool("delegate_task")
        props = tool.parameters["properties"]
        assert "agent" in props
        assert "background" in props
        assert props["background"]["default"] is False
