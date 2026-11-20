"""Regression tests for DEEP_BUGS wave 3 — #DB07.

- #DB07: `needs_compression` retorna True por `len(messages) > MAX_MESSAGES`,
  mas `compress_until_under_budget` fazia early-return no-op quando os tokens
  ja estavam sob o target — a contagem nunca era capada e o hook `pre_compact`
  disparava todo turno sem compactar nada.
"""

from __future__ import annotations

import pytest

import alpha.context as ctx
from alpha.context import (
    MAX_MESSAGES,
    compress_until_under_budget,
    needs_compression,
)


@pytest.fixture(autouse=True)
def _reset_compression_state():
    ctx._compress_consecutive_failures.set(0)
    yield
    ctx._compress_consecutive_failures.set(0)


def _tiny_messages(n: int) -> list[dict]:
    """System + (n-1) tiny user/assistant messages — way under any token cap."""
    messages = [{"role": "system", "content": "voce e um assistente"}]
    for i in range(n - 1):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"msg {i}"})
    return messages


async def _fake_stream(messages, tools, temperature, provider=None, **kwargs):
    yield {"type": "final", "content": "- resumo das mensagens anteriores"}


async def _exploding_stream(messages, tools, temperature, provider=None, **kwargs):
    raise AssertionError("stream_fn nao deveria ser chamado")
    yield  # pragma: no cover


class TestDB07CountCap:
    def test_needs_compression_true_over_max_messages(self):
        messages = _tiny_messages(MAX_MESSAGES + 1)
        assert needs_compression(messages, "deepseek") is True

    @pytest.mark.asyncio
    async def test_early_return_preserved_under_both_caps(self):
        # Tokens sob o target E contagem sob o cap → no-op sem chamar o LLM.
        messages = _tiny_messages(20)
        snapshot = [dict(m) for m in messages]
        before, after = await compress_until_under_budget(
            messages, "deepseek", _exploding_stream
        )
        assert before == after
        assert messages == snapshot

    @pytest.mark.asyncio
    async def test_count_cap_actually_shrinks_message_count(self):
        # #DB07 core: 500+ mensagens minusculas ficam bem abaixo do target de
        # tokens, mas o compressor deve capar a CONTAGEM mesmo assim.
        messages = _tiny_messages(MAX_MESSAGES + 1)
        await compress_until_under_budget(messages, "deepseek", _fake_stream)
        assert len(messages) <= MAX_MESSAGES
        assert messages[0]["role"] == "system"
        assert any("CONTEXT SUMMARY" in (m.get("content") or "") for m in messages)
        # Depois do passe, o gatilho nao deve mais disparar.
        assert needs_compression(messages, "deepseek") is False

    @pytest.mark.asyncio
    async def test_count_cap_pass_keeps_tool_pairing_invariant(self):
        # A erosao por contagem passa pelo compress_context normal, que nunca
        # pode deixar uma `role=tool` orfa no inicio da tail preservada.
        messages = _tiny_messages(MAX_MESSAGES - 3)
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": "call_1", "content": "conteudo"}
        )
        for i in range(6):
            messages.append({"role": "user", "content": f"followup {i}"})
            messages.append({"role": "assistant", "content": f"resposta {i}"})
        assert len(messages) > MAX_MESSAGES

        await compress_until_under_budget(messages, "deepseek", _fake_stream)
        assert len(messages) <= MAX_MESSAGES
        seen_tool_call_ids: set[str] = set()
        for m in messages:
            for tc in m.get("tool_calls") or []:
                seen_tool_call_ids.add(tc.get("id"))
            if m.get("role") == "tool":
                assert m.get("tool_call_id") in seen_tool_call_ids

    @pytest.mark.asyncio
    async def test_failed_pass_does_not_loop_forever(self):
        # Summary vazio → compress_context nao muda nada; o passe deve parar
        # pela guarda de progresso (contagem E tokens estagnados), sem churn.
        calls = {"n": 0}

        async def empty_stream(messages, tools, temperature, provider=None, **kwargs):
            calls["n"] += 1
            yield {"type": "final", "content": ""}

        messages = _tiny_messages(MAX_MESSAGES + 1)
        before, after = await compress_until_under_budget(
            messages, "deepseek", empty_stream
        )
        assert before == after
        assert calls["n"] == 1


class TestDB07PreCompactGating:
    @pytest.mark.asyncio
    async def test_pre_compact_fires_once_and_compression_acts(self, monkeypatch):
        # Antes do fix: com 500+ mensagens pequenas o hook disparava TODO
        # turno e o compressor era no-op. Agora: dispara, compacta, e no
        # turno seguinte (lista ja capada) nao dispara mais.
        import alpha.agent as agent_mod
        from alpha import hooks

        fired: list[dict] = []
        monkeypatch.setattr(hooks, "has_event", lambda name: name == "pre_compact")
        monkeypatch.setattr(
            hooks, "fire", lambda event, extra=None: fired.append({"event": event})
        )
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", _fake_stream)

        messages = _tiny_messages(MAX_MESSAGES + 1)

        events = [e async for e in agent_mod._maybe_compress(messages, "deepseek")]
        assert len(fired) == 1
        assert any(e["type"] == "context_compressed" for e in events)
        assert len(messages) <= MAX_MESSAGES

        events2 = [e async for e in agent_mod._maybe_compress(messages, "deepseek")]
        assert len(fired) == 1
        assert events2 == []
