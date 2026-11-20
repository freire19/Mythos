"""DEEP_MAINTAINABILITY wave 2 — behavior-preservation tests.

Refactors covered (all behavior-preserving; these tests pin the contract):

- #DM064: deny/approval gate extracted to `_gate_one`, shared between the
  parallel (`_execute_tool_calls_inner`) and sequential (`_execute_sequential`)
  dispatch modes. Event order, event shapes and `record_approval` accounting
  must be identical to the pre-refactor inline code in BOTH modes.
- #DM079: `_validate_tool_call` returns a `ValidatedCall` NamedTuple —
  positional unpacking (legacy call sites/tests) must keep working.
- #DM065: per-pair similarity extracted to `_pair_similar`, shared by
  `_are_similar` (ALL) and `_args_distinct` (ANY not) — the two must stay
  exact mirrors, including the #DP034 length gates.
- #DM075: `_cheap_len` sampling literal hoisted to `_CHEAP_LEN_SAMPLE`.
"""

from __future__ import annotations

import json

import pytest

import alpha.executor as executor_mod
from alpha.executor import (
    _CHEAP_LEN_SAMPLE,
    ValidatedCall,
    _cheap_len,
    _validate_tool_call,
    execute_tool_calls,
)
from alpha.agent.loop import (
    _are_similar,
    _args_distinct,
    _pair_similar,
    _quick_similar,
)
from alpha.tools import ToolDefinition, ToolSafety


# ─── Helpers ────────────────────────────────────────────────────────


def _mk_tool(name: str, *, safety=ToolSafety.SAFE, result=None):
    payload = result if result is not None else {"ok": True, "from": name}

    async def executor(**_kwargs):
        return payload

    return ToolDefinition(
        name=name,
        description=f"mock {name}",
        parameters={"type": "object", "properties": {}},
        safety=safety,
        executor=executor,
    )


def _tc(call_id: str, name: str, args: dict | None = None) -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args or {})}


async def _drain(gen):
    return [event async for event in gen]


# ─── #DM079 — ValidatedCall NamedTuple ──────────────────────────────


class TestValidatedCall:
    def test_positional_unpacking_still_works(self):
        """Legacy call sites unpack 6 positional elements — must not break."""
        tool = _mk_tool("mytool")
        err, name, args, safety, category, tool_def = _validate_tool_call(
            _tc("c1", "mytool"), lambda n: tool if n == "mytool" else None, None
        )
        assert err is None
        assert name == "mytool"
        assert args == {}
        assert safety == "safe"
        assert category == ""
        assert tool_def is tool

    def test_attribute_access_success(self):
        tool = _mk_tool("mytool")
        vc = _validate_tool_call(
            _tc("c1", "mytool"), lambda n: tool if n == "mytool" else None, None
        )
        assert isinstance(vc, ValidatedCall)
        assert vc.error_result is None
        assert vc.tool_name == "mytool"
        assert vc.safety_str == "safe"
        assert vc.error_category == ""
        assert vc.tool_def is tool

    def test_unknown_tool_shape_preserved(self):
        vc = _validate_tool_call(_tc("c1", "nope"), lambda n: None, None)
        assert vc.error_result == {"error": "Ferramenta desconhecida: nope"}
        assert vc.safety_str == "unknown"
        assert vc.error_category == "unknown_tool"
        assert vc.tool_def is None

    def test_parse_error_shape_preserved(self):
        tool = _mk_tool("mytool")
        vc = _validate_tool_call(
            {"id": "c1", "name": "mytool", "arguments": "{not json"},
            lambda n: tool,
            None,
        )
        assert vc.error_result is not None
        assert vc.error_category == "parse_error"
        assert vc.safety_str == "denied"


# ─── #DM064 — shared gate, sequential mode (single call) ────────────


class TestGateSequential:
    @pytest.mark.asyncio
    async def test_denied_by_rule_event_order_and_shape(self):
        tool = _mk_tool("danger")
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "danger")],
            messages,
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=lambda n: tool,
            is_denied_fn=lambda n, a: (True, "blocked by settings"),
        ))
        assert [e["type"] for e in events] == ["tool_call", "tool_result"]
        skip = events[1]
        assert skip["denied"] is True
        assert skip["id"] == "c1"
        assert skip["result"]["skipped"] is True
        assert skip["result"]["denied_by_rule"] is True
        assert skip["result"]["reason"] == "blocked by settings"
        assert skip["result"]["category"] == "denied"
        # tool message appended for the denied call
        assert len(messages) == 1
        assert messages[0]["tool_call_id"] == "c1"

    @pytest.mark.asyncio
    async def test_user_denial_records_approval_and_yields_before_callback(self, monkeypatch):
        """`approval_needed` must be yielded BEFORE the callback blocks, and
        `record_approval` must fire in the sequential mode (AUDIT_V1.8 #002)."""
        recorded: list[bool] = []
        monkeypatch.setattr(executor_mod, "record_approval", recorded.append)
        tool = _mk_tool("danger", safety=ToolSafety.DESTRUCTIVE)
        order: list[str] = []

        def callback(name, args):
            order.append("callback")
            return False

        messages: list[dict] = []
        gen = execute_tool_calls(
            [_tc("c1", "danger")],
            messages,
            needs_approval_fn=lambda n, a: True,
            approval_callback=callback,
            get_tool_fn=lambda n: tool,
        )
        events = []
        async for e in gen:
            if e["type"] == "approval_needed":
                order.append("approval_needed_event")
            events.append(e)
        assert order == ["approval_needed_event", "callback"]
        assert recorded == [False]
        assert [e["type"] for e in events] == [
            "tool_call", "approval_needed", "tool_result",
        ]
        assert events[2]["denied"] is True
        assert events[2]["result"]["reason"] == "User denied this action"

    @pytest.mark.asyncio
    async def test_user_approval_executes_and_records(self, monkeypatch):
        recorded: list[bool] = []
        monkeypatch.setattr(executor_mod, "record_approval", recorded.append)
        tool = _mk_tool("danger", safety=ToolSafety.DESTRUCTIVE, result={"done": 1})
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "danger")],
            messages,
            needs_approval_fn=lambda n, a: True,
            approval_callback=lambda n, a: True,
            get_tool_fn=lambda n: tool,
        ))
        assert recorded == [True]
        assert [e["type"] for e in events] == [
            "tool_call", "approval_needed", "tool_result",
        ]
        assert events[2]["result"] == {"done": 1}


# ─── #DM064 — shared gate, parallel mode (batch) ────────────────────


class TestGateParallel:
    @pytest.mark.asyncio
    async def test_batch_event_order_all_tool_calls_first(self):
        """Parallel mode yields ALL tool_call events before any gating."""
        tools = {
            "a": _mk_tool("a", result={"v": "a"}),
            "b": _mk_tool("b", result={"v": "b"}),
        }
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "a"), _tc("c2", "b")],
            messages,
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=tools.get,
        ))
        kinds = [e["type"] for e in events]
        assert kinds[:2] == ["tool_call", "tool_call"]
        assert sorted(e["id"] for e in events if e["type"] == "tool_result") == ["c1", "c2"]
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_partial_deny_only_blocks_denied_call(self, monkeypatch):
        recorded: list[bool] = []
        monkeypatch.setattr(executor_mod, "record_approval", recorded.append)
        tools = {
            "safe": _mk_tool("safe", result={"v": "ok"}),
            "danger": _mk_tool("danger", safety=ToolSafety.DESTRUCTIVE),
        }
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "safe"), _tc("c2", "danger")],
            messages,
            needs_approval_fn=lambda n, a: n == "danger",
            approval_callback=lambda n, a: False,
            get_tool_fn=tools.get,
        ))
        # record_approval fired exactly once (for the denied prompt)
        assert recorded == [False]
        results = {e["id"]: e for e in events if e["type"] == "tool_result"}
        assert results["c2"].get("denied") is True
        assert results["c1"]["result"] == {"v": "ok"}
        # both calls produced a tool message
        assert sorted(m["tool_call_id"] for m in messages) == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_denied_by_rule_in_batch(self):
        tools = {
            "safe": _mk_tool("safe", result={"v": "ok"}),
            "danger": _mk_tool("danger"),
        }
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "safe"), _tc("c2", "danger")],
            messages,
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=tools.get,
            is_denied_fn=lambda n, a: (n == "danger", "no way" if n == "danger" else ""),
        ))
        results = {e["id"]: e for e in events if e["type"] == "tool_result"}
        assert results["c2"]["result"]["denied_by_rule"] is True
        assert results["c2"]["result"]["reason"] == "no way"
        assert results["c1"]["result"] == {"v": "ok"}

    @pytest.mark.asyncio
    async def test_invalid_call_in_batch_keeps_event_pair_and_message(self):
        tools = {"good": _mk_tool("good", result={"v": 1})}
        messages: list[dict] = []
        events = await _drain(execute_tool_calls(
            [_tc("c1", "ghost"), _tc("c2", "good")],
            messages,
            needs_approval_fn=lambda n, a: False,
            get_tool_fn=tools.get,
        ))
        # invalid call: tool_call + tool_result pair, empty args, annotated error
        assert events[0] == {
            "type": "tool_call", "id": "c1", "name": "ghost",
            "args": {}, "safety": "unknown",
        }
        assert events[1]["type"] == "tool_result"
        assert events[1]["result"]["ok"] is False
        assert events[1]["result"]["category"] == "unknown_tool"
        assert sorted(m["tool_call_id"] for m in messages) == ["c1", "c2"]


# ─── #DM065 — _pair_similar shared by both detectors ────────────────


def _sig(name: str, args: dict) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"


class TestPairSimilarMirror:
    def test_identical_sigs(self):
        a = _sig("read_file", {"path": "/home/u/p/a.py"})
        assert _are_similar(a, a) is True
        assert _args_distinct(a, a) is False

    def test_sibling_paths_are_distinct(self):
        a = _sig("read_file", {"path": "/home/u/project/alpha/executor.py"})
        b = _sig("read_file", {"path": "/home/u/project/tests/test_x.py"})
        assert _are_similar(a, b) is False
        assert _args_distinct(a, b) is True

    def test_prefix_tail_counts_as_similar(self):
        # one value is a prefix of the other → similar (same target, deeper view)
        a = _sig("read_file", {"path": "/home/u/project"})
        b = _sig("read_file", {"path": "/home/u/project/sub"})
        assert _are_similar(a, b) is True
        assert _args_distinct(a, b) is False

    def test_length_gate_polarity_preserved(self):
        # #DP034: wildly different tail lengths (after prefix strip, with no
        # full-prefix relationship) short-circuit as NOT similar
        va, vb = "ab", "cd" + "x" * 200
        a = _sig("execute_shell", {"command": va})
        b = _sig("execute_shell", {"command": vb})
        assert _pair_similar(va, vb) is False
        assert _are_similar(a, b) is False
        assert _args_distinct(a, b) is True

    def test_full_prefix_command_still_similar(self):
        # Preserved edge: stripping "ls" from "ls xxx…" leaves one empty tail
        # → prefix rule → similar (this was true before the refactor too)
        assert _pair_similar("ls", "ls " + "x" * 200) is True

    def test_different_tools_never_similar_never_distinct(self):
        a = _sig("read_file", {"path": "/a"})
        b = _sig("write_file", {"path": "/a"})
        assert _are_similar(a, b) is False
        assert _args_distinct(a, b) is False

    def test_mirror_property_on_matrix(self):
        """For same-tool same-arity sigs, distinct == not all-pairs-similar."""
        paths = [
            "/home/u/p/a.py", "/home/u/p/b.py", "/home/u/p/a.py.bak",
            "/etc/hosts", "/home/u/p/deep/nested/file.txt",
        ]
        sigs = [_sig("read_file", {"path": p}) for p in paths]
        for sa in sigs:
            for sb in sigs:
                if sa.partition(":")[2] == sb.partition(":")[2]:
                    continue
                # exactly one of similar/distinct for single-value args
                assert _are_similar(sa, sb) is not _args_distinct(sa, sb)


class TestQuickSimilarRatioHoisted:
    def test_constant_exists_and_behavior_kept(self):
        from alpha.agent.loop import _QUICK_SIMILAR_RATIO
        assert _QUICK_SIMILAR_RATIO == 0.90
        assert _quick_similar("", "") is False          # #D113 guard
        assert _quick_similar("abc", "abc") is True     # identical prefix
        assert _quick_similar("a" * 500, "b" * 200) is False  # length gate


# ─── #DM075 — _CHEAP_LEN_SAMPLE ─────────────────────────────────────


class TestCheapLenSample:
    def test_constant_value(self):
        assert _CHEAP_LEN_SAMPLE == 50

    def test_small_collections_exact(self):
        v = ["x" * 10] * _CHEAP_LEN_SAMPLE  # at the boundary: no extrapolation
        assert _cheap_len(v) == 10 * _CHEAP_LEN_SAMPLE

    def test_extrapolation_still_covers_exact_size(self):
        # #DB16 contract: estimate never undershoots for uniform items
        v = ["y" * 20] * 300
        assert _cheap_len(v) >= 20 * 300
        d = {f"k{i:04d}": "z" * 20 for i in range(300)}
        exact = sum(len(k) + 20 + 4 for k in d)
        assert _cheap_len(d) >= exact
