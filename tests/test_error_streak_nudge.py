"""Error-streak recovery: a 1ª detecção vira nudge (tools continuam), só a
2ª força a resposta final.

Contexto (bug de campo): auditorias rodam comandos que retornam não-zero
por natureza (grep sem match, pytest indisponível). Quatro falhas seguidas
matavam o turno inteiro — "Agente parou: loop detectado (error streak)" —
jogando fora o progresso. Agora o agente recebe um [ALPHA SYSTEM NOTE] e
continua; a resposta forçada fica para reincidência.
"""

import json

import pytest

import alpha.agent as agent_mod
from alpha.tools import ToolCategory, ToolDefinition, ToolSafety


def _failing_tool_def():
    async def _fail(**_kw):
        return {"error": "boom: dependency missing"}

    return ToolDefinition(
        name="flaky_probe",
        description="always fails",
        parameters={
            "type": "object",
            "properties": {"target": {"type": "string"}},
        },
        safety=ToolSafety.SAFE,
        executor=_fail,
        category=ToolCategory.FILESYSTEM,
    )


def _get_tool(name):
    return _failing_tool_def() if name == "flaky_probe" else None


# Args bem distintos entre si para não disparar os detectores de repetição
# exata/similar antes do error streak.
_ARGS = [
    "alpha", "zebra-9", "config.py", "docs/x.md", "main", "TODO",
    "runtime", "banana", "kiwi-7", "delta", "omega", "sigma-3",
]


def _scripted_stream(script):
    """stream_chat_with_tools fake: consome `script` em ordem. Cada item:
    "fail" → final com 1 tool_call pro flaky_probe (args únicos);
    "text" → final só com conteúdo (sem tools)."""
    state = {"i": 0}

    async def fake_stream(messages, tools, temperature, **_kw):
        step = script[state["i"]] if state["i"] < len(script) else "text"
        idx = state["i"]
        state["i"] += 1
        if step == "fail":
            yield {
                "type": "final",
                "content": "",
                "tool_calls": [{
                    "id": f"c{idx}",
                    "name": "flaky_probe",
                    "arguments": json.dumps({"target": _ARGS[idx % len(_ARGS)]}),
                }],
                "error": None,
            }
        else:
            yield {
                "type": "final",
                "content": f"resposta final (step {idx})",
                "tool_calls": [],
                "error": None,
            }

    return fake_stream


async def _run(monkeypatch, script, max_iterations=30):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setattr(agent_mod, "needs_approval", lambda n, a: False)
    monkeypatch.setattr(agent_mod, "is_denied", lambda n, a: (False, ""))
    monkeypatch.setattr(
        agent_mod, "stream_chat_with_tools", _scripted_stream(script)
    )
    messages = [{"role": "user", "content": "audite o projeto"}]
    events = []
    async for event in agent_mod.run_agent(
        messages=messages,
        user_message="audite o projeto",
        provider="deepseek",
        get_tool_fn=_get_tool,
        max_iterations=max_iterations,
    ):
        events.append(event)
    return events, messages


class TestErrorStreakNudge:
    @pytest.mark.asyncio
    async def test_first_streak_nudges_and_recovers(self, monkeypatch):
        # 7 turnos com tool falhando (streak passa do threshold com o gate
        # min_calls=6 aberto) → nudge → modelo "corrige" e responde texto.
        events, messages = await _run(monkeypatch, ["fail"] * 7 + ["text"])

        nudges = [
            e for e in events
            if e.get("type") == "anomaly" and e.get("reason") == "error_streak_nudge"
        ]
        assert nudges, f"esperava anomaly de nudge, eventos: {[e.get('type') for e in events]}"

        # O turno NÃO foi morto: terminou com done normal, sem anomalia
        # terminal de loop_detected.
        assert any(e.get("type") == "done" for e in events)
        assert not any(
            e.get("type") == "anomaly" and e.get("reason") == "loop_detected"
            for e in events
        )

        # O nudge entrou como user note na conversa.
        notes = [
            m for m in messages
            if m.get("role") == "user"
            and "[ALPHA SYSTEM NOTE]" in str(m.get("content", ""))
            and "error streak" in str(m.get("content", ""))
        ]
        assert len(notes) == 1

    @pytest.mark.asyncio
    async def test_second_streak_forces_final(self, monkeypatch):
        # Reincidência: depois do nudge o modelo continua falhando →
        # caminho terminal (resposta forçada sem tools).
        events, messages = await _run(monkeypatch, ["fail"] * 14 + ["text"])

        nudges = [
            e for e in events
            if e.get("type") == "anomaly" and e.get("reason") == "error_streak_nudge"
        ]
        assert len(nudges) == 1, "nudge deve acontecer no máximo 1x por task"

        terminal = [
            e for e in events
            if e.get("type") == "anomaly" and e.get("reason") == "loop_detected"
        ]
        assert terminal, "segundo streak deve forçar a resposta final"
        assert any(e.get("type") == "done" for e in events)

    @pytest.mark.asyncio
    async def test_no_streak_no_nudge(self, monkeypatch):
        # Sem falhas suficientes: nenhum nudge, nenhuma anomalia.
        events, _messages = await _run(monkeypatch, ["fail", "fail", "text"])
        assert not any(e.get("type") == "anomaly" for e in events)
        assert any(e.get("type") == "done" for e in events)
