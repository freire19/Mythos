"""DEEP_RESILIENCE wave 2 — #D805 (deadline de turno no SDK/headless),
#D818 (teto no resumo forçado terminal) e #D819 (re-check do cap de custo
após batch de delegate_*).
"""

from __future__ import annotations

import asyncio
import math
import time

import alpha.agent as agent_mod
from alpha import _turn_budget
from alpha.agent import run_agent
from alpha.config import MAX_TURN_SECONDS
from alpha.sdk import run_task


# ─── #D805 — sdk.run_task seta o deadline do turno ───


class TestD805SdkTurnDeadline:
    def _fake_run_agent(self, seen: list[float]):
        async def fake(messages, user_message, **kwargs):
            seen.append(_turn_budget.remaining())
            yield {"type": "done", "reply": "ok"}

        return fake

    async def test_run_task_sets_finite_deadline(self, monkeypatch):
        seen: list[float] = []
        monkeypatch.setattr(agent_mod, "run_agent", self._fake_run_agent(seen))

        result = await run_task("tarefa", _tools_openai=[], _get_tool_fn=lambda n: None)

        assert result.reply == "ok"
        assert len(seen) == 1
        assert math.isfinite(seen[0])
        assert 0 < seen[0] <= MAX_TURN_SECONDS + 1
        # ContextVar não vaza após o run_task (reset em finally)
        assert _turn_budget.remaining() == math.inf

    async def test_each_turn_gets_fresh_deadline_and_resets(self, monkeypatch):
        seen: list[float] = []
        replies = iter(["parte 1\n[CONTINUE: parte 2]", "fim"])

        async def fake(messages, user_message, **kwargs):
            seen.append(_turn_budget.remaining())
            yield {"type": "done", "reply": next(replies)}

        monkeypatch.setattr(agent_mod, "run_agent", fake)

        result = await run_task(
            "tarefa", max_turns=2, _tools_openai=[], _get_tool_fn=lambda n: None
        )

        assert result.turns == 2
        assert result.reply == "fim"
        assert len(seen) == 2
        assert all(math.isfinite(r) and r <= MAX_TURN_SECONDS + 1 for r in seen)
        assert _turn_budget.remaining() == math.inf

    async def test_external_deadline_prevails(self, monkeypatch):
        seen: list[float] = []
        monkeypatch.setattr(agent_mod, "run_agent", self._fake_run_agent(seen))

        with _turn_budget.use_deadline(
            time.monotonic() + 5.0, cap_to_inherited=False
        ):
            await run_task("tarefa", _tools_openai=[], _get_tool_fn=lambda n: None)
            # O deadline externo (mais apertado) não foi sobrescrito
            assert _turn_budget.remaining() <= 5.0

        assert len(seen) == 1
        assert seen[0] <= 5.0


# ─── #D818 — resumo forçado terminal com teto de tempo ───


class TestD818ForcedSummaryTimeout:
    async def test_force_final_summary_capped_by_llm_timeout(self, monkeypatch):
        async def hanging_stream(messages, tools, temperature, provider=None):
            await asyncio.sleep(60)
            yield {"type": "final", "content": "", "tool_calls": [], "error": None}

        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", hanging_stream)
        monkeypatch.setattr(agent_mod, "LLM_TIMEOUT", 0.05)

        start = time.monotonic()
        events = [
            e
            async for e in agent_mod._force_final_summary(
                [{"role": "user", "content": "x"}],
                0.5,
                "deepseek",
                "modelo",
                "parcial",
                agent_mod.REASON_MAX_ITERATIONS,
                "limite de 1 iterações atingido",
            )
        ]
        elapsed = time.monotonic() - start

        assert elapsed < 5.0
        assert [e["type"] for e in events] == ["anomaly", "done"]
        assert events[0]["reason"] == agent_mod.REASON_MAX_ITERATIONS
        assert events[1]["reply"] == "parcial"

    async def test_force_final_summary_normal_path_unchanged(self, monkeypatch):
        async def quick_stream(messages, tools, temperature, provider=None):
            yield {"type": "content_token", "token": "resumo"}
            yield {"type": "final", "content": "resumo", "tool_calls": [], "error": None}

        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", quick_stream)

        events = [
            e
            async for e in agent_mod._force_final_summary(
                [{"role": "user", "content": "x"}],
                0.5,
                "deepseek",
                "modelo",
                "",
                agent_mod.REASON_MAX_TURN_SECONDS,
                "orçamento excedido",
            )
        ]

        assert [e["type"] for e in events] == ["token", "anomaly", "done"]
        assert events[-1]["reply"] == "resumo"


# ─── #D819 — re-check do cap de sessão após batch de delegate_* ───


def _one_tool_stream(tool_name: str, calls: list[int]):
    async def fake_stream(messages, tools, temperature, provider=None):
        calls.append(1)
        if len(calls) == 1:
            yield {
                "type": "final",
                "content": "",
                "tool_calls": [
                    {"id": "t1", "name": tool_name, "arguments": "{}"}
                ],
                "error": None,
            }
        else:
            yield {"type": "content_token", "token": "resumo"}
            yield {
                "type": "final",
                "content": "resumo",
                "tool_calls": [],
                "error": None,
            }

    return fake_stream


def _spending_executor(cost_holder: dict):
    async def fake_exec(tool_calls, messages, **kwargs):
        for tc in tool_calls:
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "content": "{}"}
            )
            cost_holder["v"] = 2.0
            yield {
                "type": "tool_result",
                "id": tc["id"],
                "name": tc["name"],
                "result": {"ok": True},
            }

    return fake_exec


class TestD819DelegateCapRecheck:
    def _setup(self, monkeypatch, tool_name: str):
        calls: list[int] = []
        cost = {"v": 0.0}
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "1.0")
        monkeypatch.delenv("ALPHA_MAX_TURN_COST_USD", raising=False)
        monkeypatch.setattr(agent_mod, "session_cost_usd", lambda: cost["v"])
        monkeypatch.setattr(
            agent_mod, "stream_chat_with_tools", _one_tool_stream(tool_name, calls)
        )
        monkeypatch.setattr(
            agent_mod, "execute_tool_calls", _spending_executor(cost)
        )
        return calls

    async def _run(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "delegue"},
        ]
        return [
            e
            async for e in run_agent(
                messages,
                "delegue",
                provider="deepseek",
                tools=[],
                max_iterations=1,
                bg_notifications=False,
            )
        ]

    async def test_delegate_breach_stops_right_after_batch(self, monkeypatch):
        calls = self._setup(monkeypatch, "delegate_parallel")

        events = await self._run()

        assert events[-1]["type"] == "error"
        assert "ALPHA_MAX_SESSION_COST_USD" in events[-1]["message"]
        # Nenhum resumo forçado depois do breach — só o stream inicial rodou
        assert len(calls) == 1

    async def test_non_delegate_batch_keeps_forced_summary(self, monkeypatch):
        calls = self._setup(monkeypatch, "write_file")

        events = await self._run()

        assert events[-1]["type"] == "done"
        assert events[-1]["reply"] == "resumo"
        assert len(calls) == 2

    async def test_delegate_under_cap_not_interrupted(self, monkeypatch):
        calls = self._setup(monkeypatch, "delegate_task")
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "10.0")

        events = await self._run()

        assert events[-1]["type"] == "done"
        assert len(calls) == 2
