"""Testes do SDK público (alpha/sdk.py) e do modo print da CLI.

Segue o padrão de mock de ``stream_chat_with_tools`` dos testes de
integração (tests/integration/test_agent_loop.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import alpha.agent as agent_mod
from alpha.sdk import TaskResult, extract_continue, run_task
from alpha.tools import ToolDefinition, ToolSafety

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_mock_stream(turns: list[tuple]):
    iter_state = {"i": 0}

    async def fake_stream(messages, tools, temperature=0.5, provider="deepseek"):
        i = iter_state["i"]
        iter_state["i"] += 1
        if i >= len(turns):
            raise AssertionError(f"Mock LLM ran out of turns after {i} calls")
        kind, content, *rest = turns[i]
        calls = rest[0] if rest else []
        for ch in content or "":
            yield {"type": "content_token", "token": ch}
        yield {"type": "final", "content": content, "tool_calls": calls, "error": None}

    return fake_stream, iter_state


def _patch_stream(monkeypatch, fake_stream):
    import alpha.llm as llm_mod

    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)
    monkeypatch.setattr(llm_mod, "stream_chat_with_tools", fake_stream)


def _mock_tool(name: str, result: dict[str, Any], safety=ToolSafety.SAFE) -> ToolDefinition:
    async def executor(**_kwargs):
        return result

    return ToolDefinition(
        name=name,
        description=f"mock {name}",
        parameters={"type": "object", "properties": {}},
        safety=safety,
        executor=executor,
    )


class TestExtractContinue:
    def test_marker_extracted(self):
        step, reply = extract_continue("fiz a parte 1\n[CONTINUE: parte 2]")
        assert step == "parte 2"
        assert reply == "fiz a parte 1"

    def test_inline_mention_ignored(self):
        text = "use a notação `[CONTINUE: foo]` para dividir trabalho em turnos"
        step, reply = extract_continue(text)
        assert step is None
        assert reply == text


class TestRunTask:
    @pytest.mark.anyio
    async def test_text_only(self, monkeypatch):
        fake, _ = _make_mock_stream([("text", "olá, tarefa concluída")])
        _patch_stream(monkeypatch, fake)

        result = await run_task("tarefa", _tools_openai=[], _get_tool_fn=lambda n: None)

        assert isinstance(result, TaskResult)
        assert result.ok
        assert result.reply == "olá, tarefa concluída"
        assert result.turns == 1
        assert result.error is None
        # Conversa termina com a resposta do assistant
        assert result.messages[-1] == {"role": "assistant", "content": "olá, tarefa concluída"}

    @pytest.mark.anyio
    async def test_tool_call_flows_through(self, monkeypatch):
        tools = {"mock_read": _mock_tool("mock_read", {"ok": True, "data": "x"})}
        fake, _ = _make_mock_stream([
            ("tools", "", [{"id": "1", "name": "mock_read", "arguments": "{}"}]),
            ("text", "pronto"),
        ])
        _patch_stream(monkeypatch, fake)

        seen: list[str] = []
        result = await run_task(
            "leia",
            on_event=lambda e: seen.append(e.get("type", "")),
            _tools_openai=[{
                "type": "function",
                "function": {"name": "mock_read", "description": "", "parameters": {}},
            }],
            _get_tool_fn=lambda n: tools.get(n),
        )

        assert result.reply == "pronto"
        event_types = [e.get("type") for e in result.events]
        assert "tool_call" in event_types
        assert "tool_result" in event_types
        assert "token" in seen  # tokens chegam ao on_event, não a result.events
        assert all(e.get("type") != "token" for e in result.events)

    @pytest.mark.anyio
    async def test_max_turns_chains_continue_marker(self, monkeypatch):
        fake, state = _make_mock_stream([
            ("text", "parte 1 feita\n[CONTINUE: faça a parte 2]"),
            ("text", "parte 2 feita"),
        ])
        _patch_stream(monkeypatch, fake)

        result = await run_task(
            "tarefa grande", max_turns=3,
            _tools_openai=[], _get_tool_fn=lambda n: None,
        )

        assert result.turns == 2
        assert result.reply == "parte 2 feita"
        assert state["i"] == 2
        # O marcador não vaza para o histórico persistido
        assert all(
            "[CONTINUE:" not in (m.get("content") or "")
            for m in result.messages if m["role"] == "assistant"
        )

    @pytest.mark.anyio
    async def test_max_turns_one_stops_at_marker(self, monkeypatch):
        fake, state = _make_mock_stream([
            ("text", "parte 1 feita\n[CONTINUE: faça a parte 2]"),
        ])
        _patch_stream(monkeypatch, fake)

        result = await run_task(
            "tarefa", max_turns=1, _tools_openai=[], _get_tool_fn=lambda n: None,
        )
        assert result.turns == 1
        assert result.reply == "parte 1 feita"
        assert state["i"] == 1

    @pytest.mark.anyio
    async def test_approval_deny_blocks_destructive(self, monkeypatch):
        ran = {"called": False}

        async def executor(**_kwargs):
            ran["called"] = True
            return {"ok": True}

        dangerous = ToolDefinition(
            name="mock_danger",
            description="mock",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.DESTRUCTIVE,
            executor=executor,
        )
        fake, _ = _make_mock_stream([
            ("tools", "", [{"id": "1", "name": "mock_danger", "arguments": "{}"}]),
            ("text", "não pude executar"),
        ])
        _patch_stream(monkeypatch, fake)

        result = await run_task(
            "apague tudo",
            approval="deny",
            _tools_openai=[{
                "type": "function",
                "function": {"name": "mock_danger", "description": "", "parameters": {}},
            }],
            _get_tool_fn=lambda n: dangerous if n == "mock_danger" else None,
        )

        assert ran["called"] is False
        assert result.reply == "não pude executar"

    @pytest.mark.anyio
    async def test_approval_auto_runs_destructive(self, monkeypatch):
        ran = {"called": False}

        async def executor(**_kwargs):
            ran["called"] = True
            return {"ok": True}

        dangerous = ToolDefinition(
            name="mock_danger",
            description="mock",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.DESTRUCTIVE,
            executor=executor,
        )
        fake, _ = _make_mock_stream([
            ("tools", "", [{"id": "1", "name": "mock_danger", "arguments": "{}"}]),
            ("text", "feito"),
        ])
        _patch_stream(monkeypatch, fake)

        result = await run_task(
            "rode",
            approval="auto",
            _tools_openai=[{
                "type": "function",
                "function": {"name": "mock_danger", "description": "", "parameters": {}},
            }],
            _get_tool_fn=lambda n: dangerous if n == "mock_danger" else None,
        )

        assert ran["called"] is True
        assert result.reply == "feito"

    @pytest.mark.anyio
    async def test_invalid_approval_raises(self):
        with pytest.raises(ValueError):
            await run_task("x", approval="talvez")

    @pytest.mark.anyio
    async def test_on_event_exception_does_not_kill_task(self, monkeypatch):
        fake, _ = _make_mock_stream([("text", "ok")])
        _patch_stream(monkeypatch, fake)

        def bad_callback(event):
            raise RuntimeError("callback quebrado")

        result = await run_task(
            "x", on_event=bad_callback, _tools_openai=[], _get_tool_fn=lambda n: None,
        )
        assert result.reply == "ok"

    @pytest.mark.anyio
    async def test_existing_messages_continued(self, monkeypatch):
        fake, _ = _make_mock_stream([("text", "continuando")])
        _patch_stream(monkeypatch, fake)

        prior = [
            {"role": "system", "content": "sys custom"},
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá"},
        ]
        result = await run_task(
            "continue", messages=prior, _tools_openai=[], _get_tool_fn=lambda n: None,
        )
        assert result.messages[0]["content"] == "sys custom"
        assert result.messages[1]["content"] == "oi"
        assert result.reply == "continuando"


def test_cli_help_exposes_print_flags():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    for flag in ("--print", "--output-format", "--max-turns", "--auto-approve"):
        assert flag in proc.stdout, flag


class TestRunPrintOutput:
    """run_print formata a saída a partir de um TaskResult — testa com o
    sdk.run_task_sync e o setup do agente mockados."""

    def _run(self, monkeypatch, capsys, output_format, result: TaskResult):
        import alpha.cli.entry as alpha_main
        from alpha import sdk

        monkeypatch.setattr(
            alpha_main, "_setup_agent_session",
            lambda p, t: (None, p, t, "sys", lambda n: None, []),
        )
        monkeypatch.setattr(sdk, "run_task_sync", lambda *a, **k: result)

        with pytest.raises(SystemExit) as exc:
            alpha_main.run_print("deepseek", 0.5, "tarefa", output_format=output_format)
        return exc.value.code, capsys.readouterr()

    def test_text_output(self, monkeypatch, capsys):
        result = TaskResult(reply="resposta final", turns=1)
        code, captured = self._run(monkeypatch, capsys, "text", result)
        assert code == 0
        assert "resposta final" in captured.out

    def test_json_output(self, monkeypatch, capsys):
        result = TaskResult(
            reply="ok", turns=2, cost_usd=0.0123,
            events=[{"type": "tool_call", "name": "read_file"}],
        )
        code, captured = self._run(monkeypatch, capsys, "json", result)
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["reply"] == "ok"
        assert payload["turns"] == 2
        assert payload["tool_calls"] == ["read_file"]
        assert payload["error"] is None

    def test_error_exits_nonzero(self, monkeypatch, capsys):
        result = TaskResult(reply="", error="boom")
        code, captured = self._run(monkeypatch, capsys, "json", result)
        assert code == 1
        assert json.loads(captured.out)["error"] == "boom"


class TestRunPrintStreamJson:
    """AUDIT_V2.3 #600: o ramo `stream-json` do run_print (emissão de um
    evento JSON por linha via on_event) tinha zero cobertura — o mock de
    run_task_sync dos outros testes nunca chamava o callback."""

    def _run(self, monkeypatch, capsys, events, result: TaskResult):
        import alpha.cli.entry as alpha_main
        from alpha import sdk

        monkeypatch.setattr(
            alpha_main, "_setup_agent_session",
            lambda p, t: (None, p, t, "sys", lambda n: None, []),
        )

        def fake_run_task_sync(*a, on_event=None, **k):
            assert on_event is not None, "stream-json deve definir on_event"
            for e in events:
                on_event(e)  # dispara a linha de emissão REAL do main.py
            return result

        monkeypatch.setattr(sdk, "run_task_sync", fake_run_task_sync)
        with pytest.raises(SystemExit) as exc:
            alpha_main.run_print(
                "deepseek", 0.5, "tarefa", output_format="stream-json"
            )
        return exc.value.code, capsys.readouterr()

    def test_stream_json_emits_one_line_per_event(self, monkeypatch, capsys):
        events = [
            {"type": "tool_call", "name": "read_file"},
            {"type": "done", "reply": "ok"},
        ]
        code, captured = self._run(
            monkeypatch, capsys, events, TaskResult(reply="ok", turns=1)
        )
        assert code == 0
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert [json.loads(ln)["type"] for ln in lines] == ["tool_call", "done"]

    def test_stream_json_default_str_serializes_exotic_values(
        self, monkeypatch, capsys
    ):
        """`default=str` cobre valores não-JSON (Path, datetime, set...)."""
        from pathlib import Path as _P

        events = [{"type": "tool_result", "path": _P("/tmp/x")}]
        code, captured = self._run(
            monkeypatch, capsys, events, TaskResult(reply="ok", turns=1)
        )
        assert code == 0
        line = [ln for ln in captured.out.splitlines() if ln.strip()][0]
        assert json.loads(line)["path"] == "/tmp/x"

    def test_stream_json_swallows_unserializable_event(self, monkeypatch, capsys):
        """Evento cuja serialização levanta (ex.: chave não-string exótica)
        é engolido sem quebrar o stream nem o exit code."""

        class _Boom:
            def __str__(self):
                raise ValueError("nope")

        events = [
            {"type": "bad", "x": _Boom()},
            {"type": "done"},
        ]
        code, captured = self._run(
            monkeypatch, capsys, events, TaskResult(reply="ok", turns=1)
        )
        assert code == 0
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert [json.loads(ln)["type"] for ln in lines] == ["done"]

    def test_stream_json_final_result_not_duplicated(self, monkeypatch, capsys):
        """stream-json NÃO imprime o resumo final (isso é do formato json) —
        só as linhas de evento."""
        code, captured = self._run(
            monkeypatch, capsys, [{"type": "done", "reply": "fim"}],
            TaskResult(reply="fim", turns=1),
        )
        assert code == 0
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 1
