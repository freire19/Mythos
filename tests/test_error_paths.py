"""Error-path tests for executor + llm + approval pipelines."""

import asyncio
import json
from pathlib import Path

import pytest

from alpha import approval
from alpha.executor import (
    _annotate_error,
    _cheap_len,
    _execute_single_tool,
    _format_result,
    _parse_and_validate_args,
    _validate_tool_call,
)
from alpha.llm import _calc_backoff
from alpha.config import RETRY, TOOL_RESULT_MAX_CHARS


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeToolDef:
    """Minimal tool definition stub for _execute_single_tool tests."""
    def __init__(self, executor_fn, parameters=None):
        self.executor = executor_fn
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.safety = type("S", (), {"value": "safe"})()


# ─── _execute_single_tool — timeout, runtime error ──────────────────


class TestExecuteSingleToolErrorPaths:
    """`_execute_single_tool` deve capturar TimeoutError e Exception
    genérica, devolvendo `_annotate_error` shape."""

    def test_tool_raises_exception_annotated_runtime(self):
        async def broken_tool(**_):
            raise RuntimeError("kaboom")

        tool_def = _FakeToolDef(broken_tool)
        result = _run(_execute_single_tool(tool_def, "broken", {}))
        assert result["ok"] is False
        assert result["category"] == "runtime"
        assert "RuntimeError" in result["error"]
        assert "kaboom" in result["error"]

    def test_tool_raises_value_error_annotated_runtime(self):
        async def bad_arg(**_):
            raise ValueError("invalid argument")

        tool_def = _FakeToolDef(bad_arg)
        result = _run(_execute_single_tool(tool_def, "bad", {}))
        assert result["category"] == "runtime"
        assert "ValueError" in result["error"]

    def test_tool_returns_dict_passes_through(self):
        async def good_tool(**_):
            return {"ok": True, "data": "x"}

        tool_def = _FakeToolDef(good_tool)
        result = _run(_execute_single_tool(tool_def, "good", {}))
        assert result == {"ok": True, "data": "x"}


# ─── _parse_and_validate_args ───────────────────────────────────────


class TestParseAndValidateArgs:
    """JSON inválido, tipo errado, campos required ausentes, filtragem de extras."""

    def test_invalid_json_returns_parse_error(self):
        tc = {"arguments": "{not valid json"}
        args, err = _parse_and_validate_args(tc, None)
        assert args is None
        assert err is not None
        assert "Invalid JSON" in err["error"]
        assert "raw_preview" in err

    def test_non_dict_args_rejected(self):
        # JSON array em vez de objeto
        tc = {"arguments": "[1, 2, 3]"}
        args, err = _parse_and_validate_args(tc, None)
        assert args is None
        assert "must be a JSON object" in err["error"]
        assert err["got_type"] == "list"

    def test_missing_required_fields(self):
        tool_def = _FakeToolDef(
            None,
            parameters={
                "type": "object",
                "properties": {"path": {}, "content": {}},
                "required": ["path", "content"],
            },
        )
        tc = {"arguments": '{"path": "x"}'}  # content missing
        args, err = _parse_and_validate_args(tc, tool_def)
        assert args is None
        assert "missing required fields" in err["error"]
        assert "content" in err["missing"]

    def test_extra_fields_silently_dropped(self):
        tool_def = _FakeToolDef(
            None,
            parameters={"type": "object", "properties": {"path": {}}},
        )
        tc = {"arguments": '{"path": "x", "extra_field": "trash"}'}
        args, err = _parse_and_validate_args(tc, tool_def)
        assert err is None
        assert args == {"path": "x"}
        assert "extra_field" not in args

    def test_empty_arguments_string_returns_empty_dict(self):
        tc = {"arguments": ""}
        args, err = _parse_and_validate_args(tc, None)
        assert err is None
        assert args == {}

    def test_tool_def_none_skips_schema_check(self):
        tc = {"arguments": '{"anything": "goes"}'}
        args, err = _parse_and_validate_args(tc, None)
        assert err is None
        assert args == {"anything": "goes"}


# ─── _validate_tool_call ─────────────────────────────────────────────


class TestValidateToolCall:
    """_validate_tool_call cobre: tool desconhecido, parse error, workspace violation."""

    def test_unknown_tool_returns_error(self):
        tc = {"name": "nonexistent_tool", "arguments": "{}", "id": "tc1"}
        err, tool_name, args, safety, category, tool_def = _validate_tool_call(
            tc, lambda _: None, workspace=None
        )
        assert err is not None
        assert err["error"].startswith("Ferramenta desconhecida")
        assert category == "unknown_tool"
        assert tool_def is None

    def test_parse_error_returns_category(self):
        tool_def = _FakeToolDef(None)
        tc = {"name": "x", "arguments": "{bad json", "id": "tc1"}
        err, _, _, safety, category, _ = _validate_tool_call(
            tc, lambda n: tool_def, workspace=None
        )
        assert err is not None
        assert category == "parse_error"
        assert safety == "denied"

    def test_workspace_violation_returns_category(self, tmp_path: Path):
        # tool_def com path param que falha workspace validation
        tool_def = _FakeToolDef(
            None,
            parameters={
                "type": "object",
                "properties": {"path": {}},
                "required": ["path"],
            },
        )
        # Workspace = tmp_path; tentamos path /tmp (fora)
        tc = {
            "name": "read_file",
            "arguments": '{"path": "/tmp"}',
            "id": "tc1",
        }
        err, tool_name, _, _, category, _ = _validate_tool_call(
            tc, lambda n: tool_def, workspace=str(tmp_path)
        )
        assert err is not None
        assert category == "violation"
        assert err["workspace_violation"] is True


# ─── _annotate_error invariant ───────────────────────────────────────


class TestAnnotateError:
    """Garantia: toda falha tem {ok: false, category: <known>}."""

    def test_setdefault_does_not_overwrite(self):
        result = _annotate_error({"ok": True, "category": "custom"}, "ignored")
        # Existing ok/category preserved
        assert result["ok"] is True
        assert result["category"] == "custom"

    def test_adds_invariant_when_missing(self):
        result = _annotate_error({"error": "x"}, "runtime")
        assert result["ok"] is False
        assert result["category"] == "runtime"
        assert result["error"] == "x"

    @pytest.mark.parametrize("category", [
        "denied", "timeout", "violation", "runtime",
        "parse_error", "unknown_tool", "tool_error",
    ])
    def test_known_categories_accepted(self, category: str):
        result = _annotate_error({"error": "x"}, category)
        assert result["category"] == category


# ─── _format_result truncation ──────────────────────────────────────


class TestFormatResultTruncation:
    """Truncation paths: cheap-len triggers preview, then minimal fallback."""

    def test_small_result_serialized_directly(self):
        result = {"path": "/x", "content": "hello"}
        out = _format_result(result, "read_file")
        parsed = json.loads(out)
        # Same shape (path may be sanitized)
        assert parsed.get("content") == "hello"

    def test_underscore_keys_stripped(self):
        result = {"path": "/x", "_previous_content": "secret", "ok": True}
        out = _format_result(result, "write_file")
        parsed = json.loads(out)
        assert "_previous_content" not in parsed
        assert "path" in parsed

    def test_large_result_uses_preview(self):
        big_string = "x" * (TOOL_RESULT_MAX_CHARS + 5000)
        result = {"content": big_string}
        out = _format_result(result, "read_file")
        parsed = json.loads(out)
        assert parsed.get("truncated") is True
        assert "preview" in parsed
        # Preview field deve ter cortado a string
        assert len(parsed["preview"]["content"]) <= 1000

    def test_huge_result_fallback_minimal(self):
        # 100 strings de TOOL_RESULT_MAX_CHARS para forcar fallback
        result = {f"k{i}": "x" * 1500 for i in range(200)}
        out = _format_result(result, "read_file")
        parsed = json.loads(out)
        assert parsed.get("truncated") is True


# _sanitize_paths já é coberto por test_code_sandbox.py::TestDeepSecurityV33ExecutorPathSanitize


# ─── _cheap_len heuristic ───────────────────────────────────────────


class TestCheapLen:
    def test_string_len(self):
        assert _cheap_len("hello") == 5

    def test_small_list(self):
        assert _cheap_len([1, 2, 3]) > 0

    def test_large_list_uses_sample(self):
        big = list(range(1000))
        result = _cheap_len(big)
        assert result > 1000  # estimated, not exact

    def test_small_dict(self):
        assert _cheap_len({"a": "x", "b": "y"}) > 0

    def test_large_dict_uses_sample(self):
        big = {str(i): "x" * 10 for i in range(500)}
        result = _cheap_len(big)
        assert result > 500


# ─── LLM retry — _calc_backoff ──────────────────────────────────────


class TestCalcBackoff:
    """Backoff calculation: exponential + jitter, capped at max_backoff."""

    def test_first_attempt_uses_initial(self):
        delay = _calc_backoff(0)
        cfg = RETRY["llm"]
        # Initial backoff * (multiplier^0) * jitter(0.5-1.0) = initial * 0.5..1.0
        assert 0 <= delay <= cfg["initial_backoff"]

    def test_max_attempt_capped(self):
        delay = _calc_backoff(100)
        cfg = RETRY["llm"]
        assert delay <= cfg["max_backoff"]

    def test_retry_after_respected(self):
        # Sem cap explicito (#D023), jitter ate 1.2x ultrapassava 5s.
        delay = _calc_backoff(0, retry_after=5.0)
        cfg = RETRY["llm"]
        assert delay <= cfg["max_backoff"]
        assert delay <= max(5.0 * 1.2, cfg["max_backoff"])

    def test_retry_after_capped_at_max(self):
        delay = _calc_backoff(0, retry_after=999.0)
        assert delay <= RETRY["llm"]["max_backoff"]


# ─── Approval — deny + needs_approval ───────────────────────────────


class TestApprovalDeny:
    """`is_denied` (hard-block) + `needs_approval` (allow/deny + defaults)."""

    def test_is_denied_no_rules_returns_false(self):
        denied, reason = approval.is_denied("execute_shell", {"command": "ls"})
        assert denied is False
        assert reason == ""

    def test_is_denied_with_matching_rule(self, monkeypatch, tmp_path: Path):
        cfg = tmp_path / ".alpha"
        cfg.mkdir()
        (cfg / "settings.json").write_text(json.dumps({
            "permissions": {"deny": ["execute_shell:sudo"]}
        }))
        monkeypatch.chdir(tmp_path)
        denied, reason = approval.is_denied("execute_shell", {"command": "sudo rm"})
        assert denied is True
        assert "sudo" in reason


@pytest.mark.parametrize("tool,args,expected", [
    ("totally_unknown", {}, True),                                          # default for unknown tools
    ("read_file", {"path": "x"}, False),                                    # AUTO_APPROVE_TOOLS
    ("write_file", {"path": "~/.bashrc", "content": "evil"}, True),         # V1.5 #006 sensitive-path gate
    ("write_file", {"path": "x", "content": ""}, True),                     # empty content anti-pattern
    ("http_request", {"url": "https://x.com", "method": "GET"}, False),     # GET auto
    ("http_request", {"url": "https://x.com", "method": "POST"}, True),     # POST requires
    ("query_database", {"query": "SELECT 1", "read_only": True}, False),    # read-only auto
    ("query_database", {"query": "DELETE FROM x", "read_only": False}, True),
    ("execute_pipeline", {"pipeline": "ls | grep .py | wc -l"}, False),     # safe pipeline auto
    ("execute_pipeline", {"pipeline": "rm foo.txt"}, True),                 # rm not in SAFE_SHELL
    ("execute_pipeline", {"pipeline": "echo $(rm /tmp/x)"}, True),          # $() expansion blocked
])
def test_needs_approval_matrix(tool: str, args: dict, expected: bool):
    """Aggregate needs_approval branches. Cache reset via conftest autouse."""
    assert approval.needs_approval(tool, args) is expected
