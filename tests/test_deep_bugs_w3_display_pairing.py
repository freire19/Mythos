"""Regression tests for the DEEP_BUGS wave-3 display/executor fixes.

- #DB05: tool_call/tool_result pareados por NOME colidem quando o batch
  chama a mesma tool 2x — o executor agora carrega um `id` estável nos
  eventos e `_run_once` pareia 1:1 por id (diff certo, files_touched certo).
- #DB16: `_cheap_len` multiplicava a amostra pela razão truncada
  (`sample * (len // 50)`), subestimando coleções até ~2x e deixando
  resultados grandes escaparem da truncagem.
"""

from __future__ import annotations

import json

import pytest

from alpha.executor import _cheap_len, execute_tool_calls
from alpha.tools import ToolDefinition, ToolSafety


# ─── Helpers ────────────────────────────────────────────────────────


def _mk_tool(name: str):
    """Tool que ecoa os args recebidos — expõe qual chamada produziu o result."""

    async def executor(**kwargs):
        return {"ok": True, "echo": kwargs}

    return ToolDefinition(
        name=name,
        description=f"mock {name}",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        safety=ToolSafety.SAFE,
        executor=executor,
    )


def _tc(call_id: str, name: str, args: dict | None = None) -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args or {})}


async def _drain(gen):
    return [event async for event in gen]


def _by_type(events, kind):
    return [e for e in events if e["type"] == kind]


# ─── #DB16 — extrapolação de _cheap_len ─────────────────────────────


class TestDB16CheapLenExtrapolation:
    def test_list_of_99_items_not_underestimated(self):
        # 99 itens: razão truncada dava multiplicador 1 (== amostra de 50),
        # subestimando ~2x. A extrapolação deve cobrir o conteúdo real.
        v = ["x" * 100] * 99
        exact = sum(len(x) for x in v)  # 9900
        assert _cheap_len(v) >= exact

    def test_dict_of_99_keys_not_underestimated(self):
        d = {f"k{i:03d}": "x" * 100 for i in range(99)}
        exact = sum(len(k) + len(val) + 4 for k, val in d.items())
        assert _cheap_len(d) >= exact

    def test_small_collections_unchanged(self):
        # <= 50 itens: soma exata, sem margem.
        v = ["ab"] * 50
        assert _cheap_len(v) == 100


# ─── #DB05 — id estável nos eventos do executor ─────────────────────


class TestDB05ExecutorEventIds:
    @pytest.mark.asyncio
    async def test_parallel_batch_events_carry_matching_ids(self):
        tools = {"edit_file": _mk_tool("edit_file")}
        calls = [
            _tc("c1", "edit_file", {"path": "a.py"}),
            _tc("c2", "edit_file", {"path": "b.py"}),
        ]
        events = await _drain(execute_tool_calls(
            calls, [],
            needs_approval_fn=lambda *_: False,
            get_tool_fn=lambda n: tools.get(n),
        ))

        tool_calls = _by_type(events, "tool_call")
        results = _by_type(events, "tool_result")
        assert [tc["id"] for tc in tool_calls] == ["c1", "c2"]
        assert {r["id"] for r in results} == {"c1", "c2"}

        # O result de cada id corresponde aos args da tool_call de MESMO id.
        args_by_id = {tc["id"]: tc["args"] for tc in tool_calls}
        for r in results:
            assert r["result"]["echo"] == args_by_id[r["id"]]

    @pytest.mark.asyncio
    async def test_sequential_single_call_carries_id(self):
        tools = {"edit_file": _mk_tool("edit_file")}
        events = await _drain(execute_tool_calls(
            [_tc("c9", "edit_file", {"path": "x.py"})], [],
            needs_approval_fn=lambda *_: False,
            get_tool_fn=lambda n: tools.get(n),
        ))
        assert _by_type(events, "tool_call")[0]["id"] == "c9"
        assert _by_type(events, "tool_result")[0]["id"] == "c9"

    @pytest.mark.asyncio
    async def test_denied_call_result_carries_id(self):
        tools = {"edit_file": _mk_tool("edit_file")}
        calls = [
            _tc("c1", "edit_file", {"path": "a.py"}),
            _tc("c2", "edit_file", {"path": "b.py"}),
        ]
        events = await _drain(execute_tool_calls(
            calls, [],
            needs_approval_fn=lambda *_: False,
            get_tool_fn=lambda n: tools.get(n),
            is_denied_fn=lambda name, args: (
                (True, "regra") if args.get("path") == "b.py" else (False, "")
            ),
        ))
        denied = [e for e in _by_type(events, "tool_result") if e.get("denied")]
        assert len(denied) == 1
        assert denied[0]["id"] == "c2"


# ─── #DB05 — pareamento 1:1 no display (_run_once) ──────────────────


class _DummyIndicator:
    def __init__(self, *a, **kw):
        pass

    def start(self, *a, **kw):
        pass

    def stop(self):
        pass

    def pause(self):
        pass

    def add_streamed_text(self, text):
        pass


class TestDB05DisplayPairing:
    @pytest.mark.asyncio
    async def test_same_tool_twice_pairs_args_by_id(self, monkeypatch):
        from alpha.cli import entry

        args_a = {"path": "a.py", "old_string": "1", "new_string": "2"}
        args_b = {"path": "b.py", "old_string": "3", "new_string": "4"}

        async def fake_run_agent(*a, **kw):
            # Ordem do batch paralelo: TODOS os tool_call, depois os results
            # — e os results podem chegar fora de ordem (gather).
            yield {"type": "tool_call", "id": "c1", "name": "edit_file",
                   "args": args_a, "safety": "destructive"}
            yield {"type": "tool_call", "id": "c2", "name": "edit_file",
                   "args": args_b, "safety": "destructive"}
            yield {"type": "tool_result", "id": "c2", "name": "edit_file",
                   "result": {"ok": True, "which": "b"}}
            yield {"type": "tool_result", "id": "c1", "name": "edit_file",
                   "result": {"ok": True, "which": "a"}}
            yield {"type": "done", "reply": "pronto"}

        printed: list[tuple[str, dict, dict | None]] = []
        summaries: list[dict] = []

        import alpha.agent as agent_mod
        import alpha.checkpoints as checkpoints_mod
        monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)
        monkeypatch.setattr(checkpoints_mod, "mark_turn", lambda label: None)
        monkeypatch.setattr(entry, "ThinkingIndicator", _DummyIndicator)
        monkeypatch.setattr(entry, "print_tool_call", lambda *a, **kw: None)
        monkeypatch.setattr(
            entry, "print_tool_result",
            lambda name, result, args=None: printed.append((name, result, args)),
        )
        monkeypatch.setattr(
            entry, "print_turn_summary",
            lambda **kw: summaries.append(kw),
        )
        monkeypatch.setattr(entry, "render_markdown", lambda text: text)
        monkeypatch.setattr(entry, "erase_active_indicator_line", lambda: None)
        monkeypatch.setattr(entry, "live_label_for_tool", lambda name, args: name)

        reply, anomaly = await entry._run_once(
            [], "edita os dois", "deepseek", 0.0, lambda n: None, [],
        )

        assert reply == "pronto"
        assert anomaly is None
        # Result de c2 (b.py) renderiza com os args de c2; c1 com os de c1 —
        # antes do fix o primeiro result fazia pop dos args do OUTRO e o
        # segundo renderizava com args=None.
        by_which = {r["which"]: args for _, r, args in printed}
        assert by_which == {"b": args_b, "a": args_a}
        # files_touched cobre AMBOS os paths (antes perdia/duplicava um).
        assert summaries[-1]["files_touched"] == {"a.py", "b.py"}
