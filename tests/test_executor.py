"""Tests for the tool executor."""

import asyncio
import json

import pytest

from alpha.executor import _execute_single_tool, _format_result, build_assistant_tool_message
from alpha.config import TOOL_RESULT_MAX_CHARS


class TestBuildAssistantMessage:
    def test_basic_message(self):
        msg = build_assistant_tool_message("hello", [
            {"id": "c1", "name": "read_file", "arguments": '{"path": "f.py"}'},
        ])
        assert msg["role"] == "assistant"
        assert msg["content"] == "hello"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"

    def test_none_content(self):
        msg = build_assistant_tool_message(None, [
            {"id": "c1", "name": "test", "arguments": "{}"},
        ])
        assert msg["content"] is None

    def test_multiple_tool_calls(self):
        msg = build_assistant_tool_message("", [
            {"id": "c1", "name": "a", "arguments": "{}"},
            {"id": "c2", "name": "b", "arguments": "{}"},
        ])
        assert len(msg["tool_calls"]) == 2


class TestFormatResult:
    def test_short_result(self):
        result = {"success": True, "data": "hello"}
        formatted = _format_result(result, "test")
        assert "hello" in formatted
        assert "truncated" not in formatted

    def test_long_result_truncated(self):
        result = {"data": "x" * (TOOL_RESULT_MAX_CHARS + 1000)}
        formatted = _format_result(result, "test")
        parsed = json.loads(formatted)
        assert parsed["truncated"] is True
        assert len(formatted) <= TOOL_RESULT_MAX_CHARS + 200

    def test_truncation_preserves_tail(self):
        # Issue #005: erro no FINAL de um log longo não pode ser cortado —
        # o clip mantém head+tail com marcador do meio omitido.
        result = {"stdout": "x" * (TOOL_RESULT_MAX_CHARS + 5000) + "THE_ERROR_AT_END"}
        formatted = _format_result(result, "execute_shell")
        parsed = json.loads(formatted)
        assert parsed["truncated"] is True
        clipped = parsed["preview"]["stdout"]
        assert clipped.startswith("xxx")
        assert clipped.endswith("THE_ERROR_AT_END")
        assert "chars omitted" in clipped

    def test_truncation_marker_reports_omitted_count(self):
        n_total = TOOL_RESULT_MAX_CHARS + 3000
        result = {"log": "a" * n_total}
        formatted = _format_result(result, "test")
        clipped = json.loads(formatted)["preview"]["log"]
        # 1000 chars preservados (600 head + 400 tail); o resto declarado.
        assert f"[{n_total - 1000} chars omitted" in clipped


class TestExecuteSingleTool:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        class MockTool:
            async def executor(self, x=1):
                return {"result": x * 2}

        result = await _execute_single_tool(MockTool(), "mock", {"x": 5})
        assert result == {"result": 10}

    @pytest.mark.asyncio
    async def test_timeout(self):
        class SlowTool:
            async def executor(self):
                await asyncio.sleep(10)
                return {"done": True}

        # Patch timeout to 0.1s for test speed
        import alpha.executor as ex
        orig = ex.TOOL_EXECUTION_TIMEOUT
        ex.TOOL_EXECUTION_TIMEOUT = 0.1
        try:
            result = await _execute_single_tool(SlowTool(), "slow", {})
            assert "error" in result
            assert "timed out" in result["error"]
        finally:
            ex.TOOL_EXECUTION_TIMEOUT = orig

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        class BadTool:
            async def executor(self):
                raise ValueError("something broke")

        result = await _execute_single_tool(BadTool(), "bad", {})
        assert "error" in result
        assert "something broke" in result["error"]
