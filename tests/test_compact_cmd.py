"""`/compact [foco]` — compressão manual do contexto.

Contratos:
- conversa curta → nada a fazer, mensagens intactas;
- compacta para system + summary + tail de 2, mutando `ctx.messages` in
  place (o /save persiste o resultado);
- o foco entra no prompt do sumarizador, sanitizado e clipado;
- sumarizador vazio → contexto mantido com aviso; exceção → erro legível;
- dispara o hook `pre_compact` com `manual: True`;
- está no catálogo e no dispatch.
"""

from __future__ import annotations

import pytest

from alpha.cli.commands import DispatchResult, _DESCRIPTIONS, _DISPATCH, compact_cmd
from alpha.cli.commands._types import ReplContext
from alpha.context import build_compression_prompt


def _ctx(n_turns: int) -> ReplContext:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_turns):
        msgs.append({"role": "user", "content": f"pergunta {i} " + "x" * 200})
        msgs.append({"role": "assistant", "content": f"resposta {i} " + "y" * 200})
    return ReplContext(
        messages=msgs, history=[], session_id="s", provider="deepseek", temperature=0.2,
        cfg={}, system_prompt="sys", tools=[], get_tool_fn=None, active_agent=None,
    )


def _fake_stream(summary: str, seen: list):
    async def stream(messages, tools, temperature, provider=None, **kw):
        seen.append(messages)
        if summary:
            yield {"type": "content_token", "token": summary}
        yield {"type": "final", "content": summary}
    return stream


@pytest.fixture
def no_hooks(monkeypatch):
    from alpha import hooks
    monkeypatch.setattr(hooks, "has_event", lambda name: False)


class TestPrompt:
    def test_focus_is_appended_sanitized(self):
        msgs = _ctx(2).messages
        p = build_compression_prompt(msgs, 1, 3, "manter caminhos‮ de arquivo")
        assert "focus on: manter caminhos de arquivo" in p and "‮" not in p
        assert p.index("focus on") < p.index("--- CONVERSATION SEGMENT ---")
        assert "focus on" not in build_compression_prompt(msgs, 1, 3)
        assert "focus on" not in build_compression_prompt(msgs, 1, 3, "   ")


class TestHandler:
    def test_short_conversation_is_noop(self, no_hooks, capsys):
        ctx = _ctx(1)
        before = list(ctx.messages)
        assert compact_cmd._handle_compact(ctx, ["/compact"]) is DispatchResult.CONTINUE
        assert ctx.messages == before
        assert "Nada para compactar" in capsys.readouterr().out

    def test_compacts_to_summary_plus_tail(self, no_hooks, monkeypatch, capsys):
        from alpha import llm

        seen: list = []
        monkeypatch.setattr(llm, "stream_chat_with_tools", _fake_stream("- resumo", seen))
        ctx = _ctx(5)
        ident = id(ctx.messages)
        assert compact_cmd._handle_compact(ctx, ["/compact", "foque", "nas", "decisões"]) is DispatchResult.CONTINUE
        assert id(ctx.messages) == ident  # mutated in place — the REPL keeps its reference
        assert [m["role"] for m in ctx.messages] == ["system", "user", "user", "assistant"]
        assert ctx.messages[0]["content"] == "sys"
        assert "[CONTEXT SUMMARY — compressed from 8 messages]" in ctx.messages[1]["content"]
        assert "- resumo" in ctx.messages[1]["content"]
        assert ctx.messages[-1]["content"].startswith("resposta 4")
        # the focus reached the summarizer
        assert "focus on: foque nas decisões" in seen[0][1]["content"]
        out = capsys.readouterr().out
        assert "Compactando 8 mensagens (foco: foque nas decisões)" in out
        assert "11 → 4 mensagens" in out and "tokens (-" in out  # 8 long messages → short summary

    def test_empty_summary_keeps_context(self, no_hooks, monkeypatch, capsys):
        from alpha import llm

        monkeypatch.setattr(llm, "stream_chat_with_tools", _fake_stream("", []))
        ctx = _ctx(4)
        before = list(ctx.messages)
        compact_cmd._handle_compact(ctx, ["/compact"])
        assert ctx.messages == before
        assert "não devolveu nada" in capsys.readouterr().out

    def test_exception_is_reported(self, no_hooks, monkeypatch, capsys):
        from alpha import llm

        async def boom(*a, **k):
            raise RuntimeError("sem rede")
            yield  # pragma: no cover

        monkeypatch.setattr(llm, "stream_chat_with_tools", boom)
        ctx = _ctx(4)
        before = list(ctx.messages)
        # compress_context swallows LLM errors itself (empty summary path)…
        compact_cmd._handle_compact(ctx, ["/compact"])
        assert ctx.messages == before
        out = capsys.readouterr().out
        assert "não devolveu nada" in out or "/compact falhou" in out

    def test_fires_pre_compact_hook(self, monkeypatch):
        from alpha import hooks, llm

        fired: list = []
        monkeypatch.setattr(hooks, "has_event", lambda name: name == "pre_compact")
        monkeypatch.setattr(hooks, "fire", lambda name, **kw: fired.append((name, kw)))
        monkeypatch.setattr(llm, "stream_chat_with_tools", _fake_stream("- ok", []))
        compact_cmd._handle_compact(_ctx(4), ["/compact"])
        assert fired and fired[0][0] == "pre_compact"
        assert fired[0][1]["extra"]["manual"] is True and fired[0][1]["extra"]["messages_count"] == 9

    def test_in_catalogue_and_dispatch(self):
        assert "/compact" in _DESCRIPTIONS and _DISPATCH["/compact"] is compact_cmd._handle_compact
