"""DEEP_PERFORMANCE wave 2 — streaming hot path do llm.py/llm_anthropic.py.

Cobre #DP026 (arguments de tool_call), #DP027 (blocos Anthropic),
#DP032 (gate do DsmlStripper) e #DP033 (cap amortizado): os fixes sao de
custo, entao os testes fixam o COMPORTAMENTO que nao pode mudar e, onde
barato, a propriedade de performance (contagem de chamadas / partes).
"""

import alpha.llm as llm
from alpha.llm import (
    DsmlStripper,
    _accumulate_tool_call_delta,
    _StreamAccumulator,
    _StreamState,
    _build_final_with_tool_calls,
)
from alpha.llm_anthropic import (
    _AnthropicStreamState,
    _apply_anthropic_event,
    _collect_thinking_blocks,
    _collect_tool_calls,
)


# ── #DP026: acumulacao de arguments de tool_call ──


class TestToolCallArgumentsAccumulation:
    def _feed(self, acc, parts):
        _accumulate_tool_call_delta(
            acc, {"index": 0, "id": "c1", "function": {"name": "write_file", "arguments": parts[0]}}
        )
        for p in parts[1:]:
            _accumulate_tool_call_delta(acc, {"index": 0, "function": {"arguments": p}})

    def test_arguments_joined_across_many_deltas(self):
        acc: dict[int, dict] = {}
        parts = ['{"path": "a.txt", "content": "'] + ["x" * 50] * 40 + ['"}']
        self._feed(acc, parts)
        assert acc[0]["arguments"] == "".join(parts)

    def test_final_event_carries_joined_arguments(self):
        state = _StreamState()
        parts = ['{"a"', ": 1", ", ", '"b": 2}']
        self._feed(state.tool_calls_acc, parts)
        final = _build_final_with_tool_calls(state)
        assert final["tool_calls"] == [
            {"id": "c1", "name": "write_file", "arguments": '{"a": 1, "b": 2}'}
        ]

    def test_read_then_more_deltas_then_read(self):
        acc: dict[int, dict] = {}
        self._feed(acc, ['{"x"'])
        assert acc[0]["arguments"] == '{"x"'
        _accumulate_tool_call_delta(acc, {"index": 0, "function": {"arguments": ": 1}"}})
        assert acc[0]["arguments"] == '{"x": 1}'

    def test_no_arguments_delta_reads_empty_string(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(acc, {"index": 0, "id": "c1", "function": None})
        assert acc[0]["arguments"] == ""

    def test_id_name_extra_content_preserved(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": 0, "function": {"arguments": "{}"}, "extra_content": {"sig": "s"}}
        )
        _accumulate_tool_call_delta(acc, {"index": 0, "id": "call_9", "function": {"name": "f"}})
        assert acc[0]["id"] == "call_9"
        assert acc[0]["name"] == "f"
        assert acc[0]["extra_content"] == {"sig": "s"}

    def test_deltas_accumulate_without_per_delta_join(self):
        acc: dict[int, dict] = {}
        self._feed(acc, ["p0"] + [f"p{i}" for i in range(1, 30)])
        entry = acc[0]
        # Sem leitura, o campo base fica intocado: nenhum join por delta.
        assert dict.__getitem__(entry, "arguments") == ""
        assert acc[0]["arguments"] == "".join(f"p{i}" for i in range(30))


# ── #DP027: blocos de streaming do Anthropic ──


def _start(state, idx, block):
    _apply_anthropic_event(
        {"type": "content_block_start", "index": idx, "content_block": block}, state
    )


def _delta(state, idx, delta):
    return _apply_anthropic_event(
        {"type": "content_block_delta", "index": idx, "delta": delta}, state
    )


class TestAnthropicBlockAccumulation:
    def test_text_deltas_still_stream_and_accumulate_content(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "text"})
        emitted = []
        for chunk in ["Hello ", "world", "!"]:
            out = _delta(state, 0, {"type": "text_delta", "text": chunk})
            if out:
                emitted.append(out)
                state.accumulated_content.add(out)
        assert "".join(emitted) == "Hello world!"
        assert state.accumulated_content.get() == "Hello world!"

    def test_text_block_does_not_duplicate_content(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "text"})
        _delta(state, 0, {"type": "text_delta", "text": "abc" * 100})
        assert not dict(state.blocks[0]).get("text")

    def test_tool_use_input_json_joined_in_collector(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "tool_use", "id": "tu1", "name": "edit_file"})
        for part in ['{"path": ', '"a.txt", ', '"content": "', "y" * 80, '"}']:
            assert _delta(state, 0, {"type": "input_json_delta", "partial_json": part}) is None
        calls = _collect_tool_calls(state.blocks)
        assert calls == [{
            "id": "tu1",
            "name": "edit_file",
            "arguments": '{"path": "a.txt", "content": "' + "y" * 80 + '"}',
        }]

    def test_tool_use_without_input_defaults_to_empty_object(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "tool_use", "id": "tu1", "name": "noop"})
        assert _collect_tool_calls(state.blocks)[0]["arguments"] == "{}"

    def test_malformed_input_json_kept_verbatim(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "tool_use", "id": "tu1", "name": "f"})
        _delta(state, 0, {"type": "input_json_delta", "partial_json": '{"broken": '})
        assert _collect_tool_calls(state.blocks)[0]["arguments"] == '{"broken": '

    def test_thinking_roundtrip_verbatim_across_split_deltas(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "thinking"})
        assert _delta(state, 0, {"type": "thinking_delta", "thinking": "pensando "}) is None
        assert _delta(state, 0, {"type": "thinking_delta", "thinking": "fundo"}) is None
        assert _delta(state, 0, {"type": "signature_delta", "signature": "sig"}) is None
        assert _delta(state, 0, {"type": "signature_delta", "signature": "123"}) is None
        assert _collect_thinking_blocks(state.blocks) == [
            {"type": "thinking", "thinking": "pensando fundo", "signature": "sig123"}
        ]

    def test_empty_thinking_deltas_not_collected(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "thinking"})
        _delta(state, 0, {"type": "thinking_delta", "thinking": ""})
        assert _collect_thinking_blocks(state.blocks) == []

    def test_redacted_thinking_still_collected(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "redacted_thinking", "data": "enc"})
        assert _collect_thinking_blocks(state.blocks) == [
            {"type": "redacted_thinking", "data": "enc"}
        ]

    def test_mismatched_delta_type_does_not_crash(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "text"})
        assert _delta(state, 0, {"type": "input_json_delta", "partial_json": "{}"}) is None
        assert _collect_tool_calls(state.blocks) == []

    def test_mixed_blocks_keep_order(self):
        state = _AnthropicStreamState()
        _start(state, 0, {"type": "thinking"})
        _delta(state, 0, {"type": "thinking_delta", "thinking": "t"})
        _start(state, 1, {"type": "tool_use", "id": "a", "name": "f1"})
        _start(state, 2, {"type": "tool_use", "id": "b", "name": "f2"})
        assert [c["id"] for c in _collect_tool_calls(state.blocks)] == ["a", "b"]


# ── #DP032: gate do DsmlStripper ──


class TestDsmlStripperGate:
    def test_plain_prose_passthrough(self):
        s = DsmlStripper()
        assert s.feed("hello ") == "hello "
        assert s.feed("world") == "world"
        assert s.flush() == ""

    def test_no_regex_scan_without_lt(self, monkeypatch):
        calls = []
        real = llm._strip_dsml

        def spy(text):
            calls.append(text)
            return real(text)

        monkeypatch.setattr(llm, "_strip_dsml", spy)
        s = DsmlStripper()
        s.feed("prosa normal sem tags ")
        s.feed("mais prosa")
        assert calls == []
        s.feed("agora <b>tag</b>")
        assert len(calls) == 1

    def test_dsml_tag_split_across_chunks_still_stripped(self):
        s = DsmlStripper()
        out = s.feed("antes <|DSM")
        out += s.feed("L|tool_calls>depois")
        out += s.flush()
        assert out == "antes depois"

    def test_emit_with_earlier_lt_and_held_tail_is_stripped(self):
        s = DsmlStripper()
        out = s.feed("a<invoke>b<x")
        out += s.feed(">c")
        out += s.flush()
        assert out == "ab<x>c"

    def test_buffer_starting_with_unclosed_lt_emits_empty(self):
        s = DsmlStripper()
        assert s.feed("<unclosed") == ""
        assert s.flush() == "<unclosed"


# ── #DP033: cap amortizado do _StreamAccumulator ──


class TestStreamAccumulatorCap:
    def test_get_never_exceeds_cap_in_streaming_flow(self):
        acc = _StreamAccumulator()
        stream = ""
        for i in range(200):
            piece = f"[{i:03d}]" * 3
            stream += piece
            acc.add(piece)
            acc.cap(100)
            assert acc.get() == stream[-100:]
            assert len(acc.get()) <= 100

    def test_deferred_trim_matches_eager_reference(self):
        acc = _StreamAccumulator()
        stream = ""
        for i in range(500):
            piece = chr(97 + i % 26) * 7
            stream += piece
            acc.add(piece)
            acc.cap(150)
        assert acc.get() == stream[-150:]

    def test_below_cap_is_untouched(self):
        acc = _StreamAccumulator()
        acc.add("abc")
        acc.cap(100)
        assert acc.get() == "abc"
        assert len(acc) == 3

    def test_bool_and_get_idempotent(self):
        acc = _StreamAccumulator()
        assert not acc
        acc.add("x" * 120)
        acc.cap(100)
        assert acc
        assert acc.get() == acc.get() == "x" * 100

    def test_overflow_region_does_not_rejoin_every_delta(self):
        acc = _StreamAccumulator()
        acc.add("x" * 100)
        acc.cap(100)
        for _ in range(4):
            acc.add("y" * 5)
            acc.cap(100)
        # Na folga (< 1.5x do cap) as partes continuam acumulando —
        # sem colapso para uma unica parte a cada delta.
        assert len(acc._parts) > 1
        assert acc.get() == "x" * 80 + "y" * 20


# ── reasoning cap integrado (call site do #DP033) ──


class TestReasoningCapIntegration:
    def test_reasoning_capped_at_50k_tail(self):
        from alpha.llm import _process_chunk, _REASONING_CONTENT_CAP

        state = _StreamState()
        piece = "r" * 10_000
        for i in range(7):
            _process_chunk(
                {"choices": [{"delta": {"reasoning_content": f"{i}" + piece}}]},
                state,
            )
        out = state.accumulated_reasoning.get()
        assert len(out) == _REASONING_CONTENT_CAP
        full = "".join(f"{i}" + piece for i in range(7))
        assert out == full[-_REASONING_CONTENT_CAP:]
