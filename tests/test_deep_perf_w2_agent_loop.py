"""DEEP_PERFORMANCE wave 2 — agent loop fixes (#DP034, #DP035, #DP036).

Foco: comportamento preservado + propriedade de performance verificavel
(spy de chamadas / formato de signature bounded).
"""

from __future__ import annotations

import json
import time

import pytest

from alpha import _turn_budget
from alpha.agent import (
    _llm_budget_clamp_setup,
    _llm_budget_timeout,
    _llm_budget_timeout_events,
)
from alpha.agent import loop as loop_mod
from alpha.agent.loop import (
    _are_similar,
    _args_distinct,
    _call_signature,
    _SIG_ARGS_CAP,
    _SIG_RAW_CAP,
)


def _sig(name: str, **args) -> str:
    return _call_signature(
        {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}
    )


# ─── #DP034 — length pre-filter before SequenceMatcher.ratio() ───


class _RatioSpy:
    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.real(*a, **kw)


@pytest.fixture
def ratio_spy(monkeypatch):
    spy = _RatioSpy(loop_mod.SequenceMatcher)
    monkeypatch.setattr(loop_mod, "SequenceMatcher", spy)
    return spy


class TestAreSimilarLengthGate:
    def test_identical_args_still_similar(self):
        a = _sig("read_file", path="/tmp/x.py")
        assert _are_similar(a, a) is True

    def test_near_identical_values_still_similar(self):
        a = _sig("execute_shell", command="pytest -1- tests/unit -q -x --tb=short")
        b = _sig("execute_shell", command="pytest -2- tests/unit -q -x --tb=short")
        assert _are_similar(a, b) is True

    def test_clearly_different_values_not_similar(self):
        a = _sig("execute_shell", command="pytest tests/ -q")
        b = _sig("execute_shell", command="grep -rn foo alpha/ | head -50")
        assert _are_similar(a, b) is False

    def test_disparate_lengths_skip_ratio(self, ratio_spy):
        a = _sig("execute_shell", command="ls -la")
        b = _sig("execute_shell", command="Z" * 280)
        assert _are_similar(a, b) is False
        assert ratio_spy.calls == 0

    def test_close_lengths_still_use_ratio(self, ratio_spy):
        a = _sig("execute_shell", command="abcdefghij-run-1")
        b = _sig("execute_shell", command="jihgfedcba-run-2")
        _are_similar(a, b)
        assert ratio_spy.calls >= 1


class TestArgsDistinctLengthGate:
    def test_identical_not_distinct(self):
        a = _sig("read_file", path="/tmp/a.md")
        assert _args_distinct(a, a) is False

    def test_different_files_distinct(self):
        a = _sig("read_file", path="/home/u/proj/docs/AUDIT_SECURITY.md")
        b = _sig("read_file", path="/home/u/proj/docs/notes/other_thing.txt")
        assert _args_distinct(a, b) is True

    def test_disparate_lengths_distinct_without_ratio(self, ratio_spy):
        a = _sig("execute_shell", command="ls")
        b = _sig("execute_shell", command="W" * 280)
        assert _args_distinct(a, b) is True
        assert ratio_spy.calls == 0

    def test_prefix_of_other_not_distinct(self):
        a = _sig("read_file", path="/home/u/proj")
        b = _sig("read_file", path="/home/u/proj/sub")
        assert _args_distinct(a, b) is False


# ─── #DP035 — bounded `_call_signature` ───


class TestCallSignatureCap:
    def test_small_args_format_unchanged(self):
        tc = {"name": "read_file", "arguments": '{"path": "/tmp/x"}'}
        assert _call_signature(tc) == 'read_file:{"path": "/tmp/x"}'

    def test_key_order_still_normalized(self):
        a = _call_signature({"name": "t", "arguments": '{"a": 1, "b": 2}'})
        b = _call_signature({"name": "t", "arguments": '{"b": 2, "a": 1}'})
        assert a == b

    def test_empty_and_invalid_args_unchanged(self):
        assert _call_signature({"name": "t", "arguments": ""}) == "t:"
        assert _call_signature({"name": "t", "arguments": "not json"}) == "t:not json"
        assert _call_signature({}) == "unknown:"

    def test_identical_large_calls_identical_signature(self):
        raw = json.dumps({"content": "x" * (_SIG_RAW_CAP + 100)})
        a = _call_signature({"name": "write_file", "arguments": raw})
        b = _call_signature({"name": "write_file", "arguments": raw})
        assert a == b

    def test_large_raw_signature_is_bounded(self):
        raw = json.dumps({"content": "x" * 300_000})
        sig = _call_signature({"name": "write_file", "arguments": raw})
        assert len(sig) < _SIG_ARGS_CAP + 100

    def test_large_raw_skips_json_roundtrip(self, monkeypatch):
        def _boom(*a, **kw):  # pragma: no cover - failure path
            raise AssertionError("json.loads called for oversized args")

        monkeypatch.setattr(loop_mod.json, "loads", _boom)
        raw = "{" + '"content": "' + "x" * _SIG_RAW_CAP + '"}'
        sig = _call_signature({"name": "write_file", "arguments": raw})
        assert sig.startswith(f"write_file:{len(raw)}:")

    def test_same_prefix_different_length_do_not_collide(self):
        base = "x" * (_SIG_RAW_CAP + 100)
        a = _call_signature({"name": "w", "arguments": json.dumps({"c": base})})
        b = _call_signature({"name": "w", "arguments": json.dumps({"c": base + "yy"})})
        assert a != b

    def test_midsize_normalized_is_capped_with_len_prefix(self):
        args = {"content": "y" * (_SIG_ARGS_CAP + 500)}
        raw = json.dumps(args)
        assert len(raw) <= _SIG_RAW_CAP
        sig = _call_signature({"name": "edit_file", "arguments": raw})
        normalized = json.dumps(args, sort_keys=True, ensure_ascii=False)
        assert sig == f"edit_file:{len(normalized)}:{normalized[:_SIG_ARGS_CAP]}"


# ─── #DP036 — lazy timeout-event construction ───


class TestLlmBudgetTimeout:
    def test_none_without_deadline(self):
        assert _llm_budget_timeout() is None

    def test_clamped_with_deadline(self):
        with _turn_budget.use_deadline(
            time.monotonic() + 2.0, cap_to_inherited=False
        ):
            to = _llm_budget_timeout()
            assert to is not None
            assert 0 < to <= 2.0

    def test_events_shape_matches_legacy_contract(self):
        anom, done, log = _llm_budget_timeout_events(
            "test_reason", "Test label", "partial reply", 3.0
        )
        assert anom["type"] == "anomaly"
        assert anom["reason"] == "test_reason"
        assert "Test label" in anom["detail"]
        assert "3s" in anom["detail"]
        assert done == {"type": "done", "reply": "partial reply"}
        assert "Test label" in log
        assert "timed out" in log
        assert "3.0s" in log

    def test_compat_wrapper_composes_both_halves(self):
        with _turn_budget.use_deadline(
            time.monotonic() + 5.0, cap_to_inherited=False
        ):
            to, anom, done, log = _llm_budget_clamp_setup("r", "L", "x")
            assert to is not None
            lazy_anom, lazy_done, lazy_log = _llm_budget_timeout_events(
                "r", "L", "x", to
            )
            assert anom == lazy_anom
            assert done == lazy_done
            assert log == lazy_log
