"""Regression tests for the DEEP_BUGS wave-3 llm.py fixes.

- #DB04: `function`/`delta` com null explicito no chunk SSE nao podem
  virar `None.get(...)` -> AttributeError fora dos handlers.
"""

from __future__ import annotations

from alpha.llm import _StreamState, _accumulate_tool_call_delta, _process_chunk


class TestDB04NullFunctionDelta:
    def test_new_slot_with_function_null(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(acc, {"index": 0, "id": "call_1", "function": None})
        assert acc[0]["name"] == ""
        assert acc[0]["arguments"] == ""

    def test_existing_slot_with_function_null(self):
        acc: dict[int, dict] = {}
        _accumulate_tool_call_delta(
            acc, {"index": 0, "id": "call_1", "function": {"name": "read_file"}}
        )
        _accumulate_tool_call_delta(acc, {"index": 0, "function": None})
        assert acc[0]["name"] == "read_file"

    def test_process_chunk_with_delta_null(self):
        state = _StreamState()
        chunk = {"choices": [{"delta": None, "finish_reason": "stop"}]}
        assert _process_chunk(chunk, state) is None
        assert state.last_finish_reason == "stop"

    def test_process_chunk_with_tool_call_function_null(self):
        state = _StreamState()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "id": "call_x", "function": None}]
                    }
                }
            ]
        }
        assert _process_chunk(chunk, state) is None
        assert state.tool_calls_acc[0]["id"] == "call_x"

    def test_process_chunk_normal_flow_unchanged(self):
        state = _StreamState()
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    }
                }
            ]
        }
        _process_chunk(chunk, state)
        _process_chunk(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": 'th": "x"}'}}
                            ]
                        }
                    }
                ]
            },
            state,
        )
        assert state.tool_calls_acc[0]["name"] == "read_file"
        assert state.tool_calls_acc[0]["arguments"] == '{"path": "x"}'
