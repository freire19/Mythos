"""DEEP_MAINTAINABILITY wave 2 — #DM067: refactor de entry.py.

Locks para o comportamento preservado após a extração de `_TurnState` +
tabela de dispatch de eventos em `_run_once` e dos helpers
`_render_prompt`/`_handle_auto_continue` de `run_repl`:

- `import time` único no topo do módulo (antes: dois aliases `_time`/`_t`
  importados DENTRO de `_run_once`);
- eventos idênticos: token/tool_call/tool_result/context_compressed/done/
  error/anomaly produzem o mesmo reply, os mesmos contadores de summary e
  a mesma anomalia terminal;
- anomalias mid-turn (`error_streak_nudge`/`bg_task_finished`) continuam
  informativas — não viram anomalia de fim de turno;
- Ctrl+C re-levanta e o summary sai com anomalia sintética "interrupted";
- deadline do turno é resetada em qualquer caminho de saída;
- `_handle_auto_continue` respeita o cap (#DL218) e enfileira em append
  (input do usuário digitado durante o turno roda primeiro);
- `_render_prompt` anuncia cada threshold uma vez só e colore o prompt
  conforme plan mode / auto-accept.
"""

from __future__ import annotations

import pytest

from alpha.cli import entry


# ─── helpers ────────────────────────────────────────────────────────


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


def _patch_display(monkeypatch, summaries=None, results=None, errors=None):
    """Silencia o display de entry.py e captura summary/results/errors."""
    import alpha.checkpoints as checkpoints_mod

    monkeypatch.setattr(checkpoints_mod, "mark_turn", lambda label: None)
    monkeypatch.setattr(entry, "ThinkingIndicator", _DummyIndicator)
    monkeypatch.setattr(entry, "print_tool_call", lambda *a, **kw: None)
    monkeypatch.setattr(
        entry, "print_tool_result",
        lambda name, result, args=None: (
            results.append((name, result, args)) if results is not None else None
        ),
    )
    monkeypatch.setattr(
        entry, "print_turn_summary",
        lambda **kw: summaries.append(kw) if summaries is not None else None,
    )
    monkeypatch.setattr(
        entry, "print_error",
        lambda msg: errors.append(msg) if errors is not None else None,
    )
    monkeypatch.setattr(entry, "print_context_compressed", lambda *a: None)
    monkeypatch.setattr(entry, "print_phase", lambda *a: None)
    monkeypatch.setattr(entry, "render_markdown", lambda text: text)
    monkeypatch.setattr(entry, "erase_active_indicator_line", lambda: None)
    monkeypatch.setattr(entry, "live_label_for_tool", lambda name, args: name)


def _patch_agent(monkeypatch, events):
    import alpha.agent as agent_mod

    async def fake_run_agent(*a, **kw):
        for ev in events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev

    monkeypatch.setattr(agent_mod, "run_agent", fake_run_agent)


async def _run(monkeypatch, events, summaries=None, results=None, errors=None):
    _patch_display(
        monkeypatch, summaries=summaries, results=results, errors=errors
    )
    _patch_agent(monkeypatch, events)
    return await entry._run_once(
        [], "tarefa", "deepseek", 0.0, lambda n: None, [],
    )


# ─── import time único (parte OBRIGATÓRIA do #DM067) ────────────────


class TestSingleTimeImport:
    def test_no_aliased_time_imports(self):
        import inspect

        src = inspect.getsource(entry)
        assert "import time as" not in src
        assert src.count("\nimport time\n") == 1

    def test_module_has_time_global(self):
        import time as _stdlib_time

        assert entry.time is _stdlib_time


# ─── _run_once: comportamento preservado ────────────────────────────


class TestRunOncePreserved:
    @pytest.mark.asyncio
    async def test_full_event_flow_counters_and_reply(self, monkeypatch, capsys):
        summaries, results = [], []
        events = [
            {"type": "token", "text": "olá "},
            {"type": "token", "text": "mundo"},
            {"type": "tool_call", "id": "c1", "name": "edit_file",
             "args": {"path": "x.py", "old_string": "a", "new_string": "b"},
             "safety": "destructive"},
            {"type": "tool_result", "id": "c1", "name": "edit_file",
             "result": {"ok": True}},
            {"type": "tool_call", "id": "c2", "name": "read_file",
             "args": {"path": "y.py"}, "safety": "safe"},
            {"type": "tool_result", "id": "c2", "name": "read_file",
             "result": {"ok": False, "error": "boom"}},
            {"type": "context_compressed", "before": 100, "after": 50},
            {"type": "done", "reply": "ignorado — houve stream"},
        ]
        reply, anomaly = await _run(
            monkeypatch, events, summaries=summaries, results=results
        )

        # Reply vem dos tokens streamados; `done` NÃO sobrescreve.
        assert reply == "olá mundo"
        assert anomaly is None
        assert len(summaries) == 1
        s = summaries[0]
        # ok:True conta como tool; ok:False conta como erro (não tool).
        assert s["tools_count"] == 1
        assert s["errors_count"] == 1
        # files_touched só para write-tools com resultado ok.
        assert s["files_touched"] == {"x.py"}
        assert s["anomaly"] is None
        # Texto streamado chegou ao stdout (via flush_text).
        assert "olá mundo" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_done_fallback_when_nothing_streamed(self, monkeypatch):
        reply, anomaly = await _run(
            monkeypatch, [{"type": "done", "reply": "resposta do done"}],
            summaries=[],
        )
        assert reply == "resposta do done"
        assert anomaly is None

    @pytest.mark.asyncio
    async def test_error_event_counts_and_prints(self, monkeypatch):
        summaries, errors = [], []
        reply, anomaly = await _run(
            monkeypatch,
            [{"type": "error", "message": "explodiu"},
             {"type": "done", "reply": ""}],
            summaries=summaries, errors=errors,
        )
        assert errors == ["explodiu"]
        assert summaries[0]["errors_count"] == 1
        assert anomaly is None

    @pytest.mark.asyncio
    async def test_terminal_anomaly_is_returned(self, monkeypatch):
        summaries = []
        reply, anomaly = await _run(
            monkeypatch,
            [{"type": "anomaly", "reason": "budget_exceeded",
              "detail": "1h estourada"},
             {"type": "done", "reply": "parcial"}],
            summaries=summaries,
        )
        assert anomaly == {"reason": "budget_exceeded", "detail": "1h estourada"}
        assert summaries[0]["anomaly"] == anomaly

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["error_streak_nudge", "bg_task_finished"])
    async def test_midturn_anomaly_is_informative_only(self, monkeypatch, reason):
        summaries = []
        reply, anomaly = await _run(
            monkeypatch,
            [{"type": "anomaly", "reason": reason, "detail": "aviso"},
             {"type": "done", "reply": "segue o turno"}],
            summaries=summaries,
        )
        # Mid-turn NÃO vira anomalia terminal — o turno continuou.
        assert anomaly is None
        assert summaries[0]["anomaly"] is None
        assert reply == "segue o turno"

    @pytest.mark.asyncio
    async def test_unknown_event_type_is_noop(self, monkeypatch):
        reply, anomaly = await _run(
            monkeypatch,
            [{"type": "algo_novo", "x": 1}, {"type": "done", "reply": "ok"}],
            summaries=[],
        )
        assert reply == "ok"
        assert anomaly is None

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_reraises_with_synthetic_anomaly(
        self, monkeypatch
    ):
        summaries = []
        events = [{"type": "token", "text": "meio"}, KeyboardInterrupt()]
        with pytest.raises(KeyboardInterrupt):
            await _run(monkeypatch, events, summaries=summaries)
        # Summary saiu mesmo com o abort, com a anomalia sintética.
        assert len(summaries) == 1
        assert summaries[0]["anomaly"]["reason"] == "interrupted"

    @pytest.mark.asyncio
    async def test_turn_deadline_reset_on_all_paths(self, monkeypatch):
        from alpha import _turn_budget

        assert _turn_budget._turn_deadline.get() is None
        await _run(monkeypatch, [{"type": "done", "reply": "ok"}], summaries=[])
        assert _turn_budget._turn_deadline.get() is None

        with pytest.raises(KeyboardInterrupt):
            await _run(monkeypatch, [KeyboardInterrupt()], summaries=[])
        assert _turn_budget._turn_deadline.get() is None

    @pytest.mark.asyncio
    async def test_pairing_by_id_still_holds(self, monkeypatch):
        """#DB05 não regrediu: results fora de ordem pareiam pelos ids."""
        results, summaries = [], []
        args_a = {"path": "a.py", "old_string": "1", "new_string": "2"}
        args_b = {"path": "b.py", "old_string": "3", "new_string": "4"}
        events = [
            {"type": "tool_call", "id": "c1", "name": "edit_file",
             "args": args_a, "safety": "destructive"},
            {"type": "tool_call", "id": "c2", "name": "edit_file",
             "args": args_b, "safety": "destructive"},
            {"type": "tool_result", "id": "c2", "name": "edit_file",
             "result": {"ok": True, "which": "b"}},
            {"type": "tool_result", "id": "c1", "name": "edit_file",
             "result": {"ok": True, "which": "a"}},
            {"type": "done", "reply": "pronto"},
        ]
        await _run(monkeypatch, events, summaries=summaries, results=results)
        by_which = {r["which"]: args for _, r, args in results}
        assert by_which == {"b": args_b, "a": args_a}
        assert summaries[-1]["files_touched"] == {"a.py", "b.py"}


# ─── _render_prompt ─────────────────────────────────────────────────


class TestRenderPrompt:
    def _patch_context(self, monkeypatch, used, limit):
        import alpha.context as ctx_mod

        monkeypatch.setattr(
            ctx_mod, "estimate_messages_tokens", lambda messages: used
        )
        monkeypatch.setattr(ctx_mod, "get_context_limit", lambda provider: limit)
        monkeypatch.setattr(
            entry, "format_context_indicator", lambda messages, provider: ""
        )

    def test_threshold_announced_once(self, monkeypatch):
        warnings = []
        self._patch_context(monkeypatch, used=75, limit=100)
        monkeypatch.setattr(
            entry, "print_context_warning",
            lambda pct, used, limit: warnings.append(pct),
        )
        announced: set[int] = set()
        entry._render_prompt([], "deepseek", announced)
        entry._render_prompt([], "deepseek", announced)
        # 75% dispara a faixa de 50 primeiro; segunda chamada anuncia a de
        # 70; a terceira não repete nenhuma (comportamento do loop+break).
        entry._render_prompt([], "deepseek", announced)
        assert warnings == [75, 75]
        assert announced == {50, 70}

    def test_prompt_color_by_mode(self, monkeypatch):
        # `c()` descarta ANSI fora de TTY — captura a cor pedida em vez de
        # procurar escapes na string final.
        from alpha import plan_mode
        from alpha.display import C

        colors: list[str] = []
        self._patch_context(monkeypatch, used=0, limit=100)
        monkeypatch.setattr(entry, "print_context_warning", lambda *a: None)
        monkeypatch.setattr(
            entry, "c",
            lambda color, text: (colors.append(color), text)[1],
        )
        prompt = entry._render_prompt([], "deepseek", set())
        assert "❯" in prompt
        assert colors[-1] == C.VIOLET + C.BOLD
        monkeypatch.setattr(plan_mode, "is_active", lambda: True)
        entry._render_prompt([], "deepseek", set())
        assert colors[-1] == C.CYAN + C.BOLD


# ─── _handle_auto_continue (#DL218 preservado) ──────────────────────


class TestHandleAutoContinue:
    def test_below_cap_appends_and_increments(self, capsys):
        queued = ["digitado pelo usuário"]
        n = entry._handle_auto_continue("próximo passo", 0, 10, queued)
        assert n == 1
        # append, não insert(0): o input do usuário roda primeiro.
        assert queued == ["digitado pelo usuário", "próximo passo"]
        assert "continuando (1/10)" in capsys.readouterr().out

    def test_at_cap_warns_and_does_not_queue(self, capsys):
        queued: list[str] = []
        n = entry._handle_auto_continue("mais", 10, 10, queued)
        assert n == 10
        assert queued == []
        assert "limite de auto-continues atingido (10)" in capsys.readouterr().out
