"""Regression tests for the DEEP_LOGIC V2.0 BAIXO fixes.

Covers #DL-002, #DL-003, #DL-004, #DL-014, #015, #017. (#013 dry-run count,
#014 deploy unknown, #016 update OSError, #DL-005 sub-agent partial are
covered elsewhere / by review where a unit harness is impractical.)
"""
from __future__ import annotations

import re

import pytest


# ── #DL-004: hook matcher anchors to the full tool name ──

def test_hook_matcher_fullmatch_not_substring():
    from alpha.hooks import Hook, _matches
    plain = Hook(event="pre_tool", command="x", matcher=re.compile("write"))
    # "write" must NOT match write_file (substring) nor rewrite (the footgun).
    assert _matches(plain, "write_file") is False
    assert _matches(plain, "rewrite") is False
    assert _matches(plain, "write") is True

    alt = Hook(event="pre_tool", command="x", matcher=re.compile("write_file|edit_file"))
    assert _matches(alt, "write_file") is True
    assert _matches(alt, "edit_file") is True
    assert _matches(alt, "edit_file_2") is False  # full name, not prefix

    none = Hook(event="pre_tool", command="x", matcher=None)
    assert _matches(none, "anything") is True


def test_hook_substring_matcher_logs_migration_warning(caplog):
    import logging
    from alpha.hooks import Hook, _matches
    # A unique pattern avoids cross-test pollution of the warned-once set.
    h = Hook(event="pre_tool", command="x", matcher=re.compile("uniqfrag"))
    with caplog.at_level(logging.WARNING):
        # substring match under the OLD behavior, no full match now → warns once
        assert _matches(h, "tool_uniqfrag_v2") is False
    assert any("no longer fires" in r.getMessage() for r in caplog.records)


# ── #DL-014: list-typed path params validated per element ──

def test_git_files_list_outside_workspace_rejected(tmp_path):
    from alpha.agents.workspace import validate_args
    ok, _new, err = validate_args(
        str(tmp_path), "git_operation",
        {"action": "blame", "files": ["/etc/passwd"]},
    )
    assert ok is False
    assert "outside" in err


def test_git_files_list_inside_workspace_rewritten(tmp_path):
    from alpha.agents.workspace import validate_args
    (tmp_path / "a.py").write_text("x")
    ok, new, err = validate_args(
        str(tmp_path), "git_operation",
        {"action": "add", "files": ["a.py"]},
    )
    assert ok is True and err == ""
    assert new["files"][0].endswith("/a.py")
    assert str(tmp_path) in new["files"][0]


def test_git_empty_files_list_not_defaulted(tmp_path):
    from alpha.agents.workspace import validate_args
    ok, new, _ = validate_args(
        str(tmp_path), "git_operation", {"action": "status", "files": []},
    )
    assert ok is True
    # empty list stays empty — must NOT become [str(ws)]
    assert new["files"] == []


# ── #DL-003: every assistant.tool_calls is sanitized, not just the last ──

def test_sanitize_drops_earlier_orphan_assistant():
    from alpha.history import _sanitize_for_save
    msgs = [
        {"role": "user", "content": "q1"},
        # EARLIER assistant: 2 calls, only 1 responded → orphan
        {"role": "assistant", "tool_calls": [{"id": "a1"}, {"id": "a2"}]},
        {"role": "tool", "tool_call_id": "a1", "content": "ok"},
        # LATER assistant: complete pair
        {"role": "assistant", "tool_calls": [{"id": "b1"}]},
        {"role": "tool", "tool_call_id": "b1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    out = _sanitize_for_save(msgs)
    ids_present = {
        tc["id"]
        for m in out if m.get("role") == "assistant" and m.get("tool_calls")
        for tc in m["tool_calls"]
    }
    # The earlier orphan (a1/a2) and its partial response must be gone...
    assert "a1" not in ids_present and "a2" not in ids_present
    assert not any(m.get("role") == "tool" and m.get("tool_call_id") in {"a1", "a2"} for m in out)
    # ...the later complete pair survives.
    assert "b1" in ids_present
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "b1" for m in out)


# ── #015: exact-repeat detector catches historical repeats (A→A→…→B) ──

def test_loop_detects_historical_repeat_not_in_current_turn():
    from alpha.agent.loop import _detect_loop, _HISTORICAL_REPEAT_THRESHOLD
    a = "read_file:/proj/a"
    # A saturated to the HISTORICAL threshold (2×); current turn is a distinct B.
    recent = [a] * _HISTORICAL_REPEAT_THRESHOLD + ["other_tool:/proj/b"]
    reason = _detect_loop(["other_tool:/proj/b"], recent, [])
    assert reason is not None
    assert "exact repeat" in reason


def test_loop_historical_below_threshold_does_not_fire():
    # #015 softening (code-review): a sig repeated only _MAX_REPEAT_CALLS times
    # in HISTORY (not the current turn) must NOT force-final — that's a routine
    # re-read between edits, not a live loop. Only the stricter 2× historical
    # threshold fires when decoupled from the current turn.
    from alpha.agent.loop import _detect_loop, _MAX_REPEAT_CALLS
    a = "read_file:/proj/a"
    recent = [a] * _MAX_REPEAT_CALLS + ["b:1", "c:2", "d:3"]
    # Current turn is a distinct, non-repeating call.
    assert _detect_loop(["d:3"], recent, []) is None


# ── #017: undetectable test framework returns the {ok:false} invariant ──

@pytest.mark.asyncio
async def test_run_tests_undetectable_framework_annotated(tmp_path, monkeypatch):
    import alpha.tools._composite_tests as ct
    # No framework markers, no test files → auto-detect fails.
    monkeypatch.setattr(ct, "_resolve_target", lambda path: (tmp_path, None))
    out = await ct._run_tests(path=str(tmp_path), framework="auto")
    assert out.get("ok") is False
    assert out.get("category") == "runtime"
    assert "error" in out


# ── #DL-002: overflow recovery that shrinks nothing emits an anomaly ──

@pytest.mark.asyncio
async def test_overflow_retry_signals_when_nothing_shrinks(monkeypatch):
    import alpha.agent as agent_mod

    async def fake_compress(messages, provider, fn, target_tokens, max_passes):
        return None, 1000  # tokens_after == tokens_before → no shrink

    monkeypatch.setattr(agent_mod, "compress_until_under_budget", fake_compress)
    monkeypatch.setattr(agent_mod, "estimate_messages_tokens", lambda m: 1000)
    monkeypatch.setattr(agent_mod, "get_context_limit", lambda p: 2000)

    events = [
        e async for e in agent_mod._handle_overflow_retry(
            [{"role": "user", "content": "x"}], "deepseek"
        )
    ]
    assert any(
        e.get("type") == "anomaly" and e.get("reason") == "context_overflow_unrecoverable"
        for e in events
    )
