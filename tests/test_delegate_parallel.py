"""AUDIT_V2.3 #601 — fan-out, circuit breaker e ramo de exceção do
`_delegate_parallel` (a mitigação AUDIT_V2.2 #012 não tinha nenhum teste).

`_run_subagent` é mockado no módulo — nenhum LLM/subprocess real.
"""

from __future__ import annotations

import json

import pytest

import alpha.tools.delegate_tools as dt
from alpha.config import FEATURES


@pytest.fixture(autouse=True)
def _delegation_on(monkeypatch):
    monkeypatch.delenv("ALPHA_DELEGATED", raising=False)
    monkeypatch.setitem(FEATURES, "multi_agent_enabled", True)
    monkeypatch.setitem(FEATURES, "delegate_tool_enabled", True)


class TestFanOutHappyPath:
    @pytest.mark.asyncio
    async def test_all_tasks_complete(self, monkeypatch):
        seen: list[str] = []

        async def fake_subagent(task, context, tools_filter, provider, label=""):
            seen.append(task)
            return {"status": "completed", "result": f"ok:{task}"}

        monkeypatch.setattr(dt, "_run_subagent", fake_subagent)
        out = await dt._delegate_parallel(json.dumps(["t1", "t2", "t3"]))

        assert out["total_tasks"] == 3
        assert out["succeeded"] == 3
        assert out["failed"] == 0
        assert sorted(seen) == ["t1", "t2", "t3"]
        # task_index/task anexados a cada resultado, na ordem submetida.
        assert [r["task_index"] for r in out["results"]] == [0, 1, 2]
        assert all(r["status"] == "completed" for r in out["results"])


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_breaker_short_circuits_remaining_tasks(self, monkeypatch):
        """Com max_parallel=1 (determinístico): threshold = max(2,1) = 2.
        Após 2 falhas o breaker abre e as demais tasks NÃO chamam
        `_run_subagent` — viram status "skipped"."""
        monkeypatch.setitem(FEATURES, "max_parallel_agents", 1)
        calls = {"n": 0}

        async def failing_subagent(task, context, tools_filter, provider, label=""):
            calls["n"] += 1
            return {"status": "failed", "error": "provider morto"}

        monkeypatch.setattr(dt, "_run_subagent", failing_subagent)
        tasks = [f"t{i}" for i in range(8)]
        out = await dt._delegate_parallel(json.dumps(tasks))

        # Prova do short-circuit (nota da review): só o threshold executa.
        assert calls["n"] == 2
        assert calls["n"] < 8
        statuses = [r["status"] for r in out["results"]]
        assert statuses.count("failed") == 2
        assert statuses.count("skipped") == 6
        assert out["succeeded"] == 0
        assert out["failed"] == 8  # failed + skipped contam como não-sucesso
        skipped = [r for r in out["results"] if r["status"] == "skipped"]
        assert all("circuit breaker" in r["error"] for r in skipped)

    @pytest.mark.asyncio
    async def test_small_batch_never_trips(self, monkeypatch):
        """Batch <= threshold: mesmo com falhas, nada é skipped."""
        monkeypatch.setitem(FEATURES, "max_parallel_agents", 3)

        async def failing_subagent(task, context, tools_filter, provider, label=""):
            return {"status": "failed", "error": "x"}

        monkeypatch.setattr(dt, "_run_subagent", failing_subagent)
        out = await dt._delegate_parallel(json.dumps(["a", "b"]))
        assert [r["status"] for r in out["results"]] == ["failed", "failed"]


class TestExceptionBranch:
    @pytest.mark.asyncio
    async def test_raised_exception_becomes_failed_result(self, monkeypatch):
        """Ramo `isinstance(r, Exception)` do gather(return_exceptions=True):
        um sub-agent que LEVANTA (não retorna dict) vira status failed com a
        mensagem, sem derrubar os irmãos."""

        async def sometimes_raising(task, context, tools_filter, provider, label=""):
            if task == "boom":
                raise RuntimeError("explodiu")
            return {"status": "completed", "result": "ok"}

        monkeypatch.setattr(dt, "_run_subagent", sometimes_raising)
        out = await dt._delegate_parallel(json.dumps(["ok1", "boom", "ok2"]))

        by_task = {r["task"]: r for r in out["results"]}
        assert by_task["boom"]["status"] == "failed"
        assert "explodiu" in by_task["boom"]["error"]
        assert by_task["ok1"]["status"] == "completed"
        assert by_task["ok2"]["status"] == "completed"
        assert out["succeeded"] == 2
        assert out["failed"] == 1


class TestNormalizeGatherHelper:
    """AUDIT_V2.3 #507: helper único para Exception -> {status: failed}."""

    def test_parallel_and_consensus_shapes(self):
        results = [{"status": "completed"}, RuntimeError("x")]
        par = dt._normalize_gather(
            results, extra=lambda i: {"task_index": i, "task": f"t{i}"}
        )
        assert par[0] == {"status": "completed"}
        assert par[1] == {"status": "failed", "error": "x", "task_index": 1, "task": "t1"}
        cons = dt._normalize_gather(results, extra=lambda i: {"agent_index": i, "result": ""})
        assert cons[1] == {"status": "failed", "error": "x", "agent_index": 1, "result": ""}
