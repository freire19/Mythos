"""Regression tests for the DEEP_BUGS V5.0 BAIXO fixes (#D105/#D108/#D110/
#D111/#D112/#D113/#D115). Logging-only fixes (#D103/#D118/#D119/#D120/#D106)
are covered by inspection, not asserted here.
"""
from __future__ import annotations

import os

import pytest


# ── #D113: two empty previews are not "similar" ──

def test_quick_similar_two_empty_is_false():
    from alpha.agent.loop import _quick_similar
    assert _quick_similar("", "") is False
    # non-empty identical still similar
    assert _quick_similar("abc", "abc") is True


# ── #D115: malformed tools_filter must not silently zero all tools ──

def test_tools_filter_comma_only_does_not_zero_tools():
    from alpha.tools.delegate_tools import _build_subagent_tools
    base_tools, base_allowed = _build_subagent_tools("", set())
    assert base_allowed is None
    for bad in (",", "  ", " , , "):
        tools, allowed = _build_subagent_tools(bad, set())
        # Treated as "no filter", not as an empty allow-set.
        assert allowed is None, f"{bad!r} should yield no filter"
        assert len(tools) == len(base_tools)


def test_tools_filter_real_value_still_filters():
    from alpha.tools.delegate_tools import _build_subagent_tools
    all_tools, _ = _build_subagent_tools("", set())
    if not all_tools:
        pytest.skip("no tools registered")
    name = all_tools[0]["function"]["name"]
    tools, allowed = _build_subagent_tools(f"{name}, ,", set())
    assert allowed == {name}
    assert [t["function"]["name"] for t in tools] == [name]


# ── #D105: env overrides validated against a whitelist ──

def test_validated_env_falls_back_on_typo(monkeypatch):
    from alpha.config import _validated_env
    monkeypatch.setenv("X_TEST_ENV", "maximum")  # typo for "max"
    assert _validated_env("X_TEST_ENV", "max", {"max", "high"}) == "max"
    monkeypatch.setenv("X_TEST_ENV", "HIGH")
    assert _validated_env("X_TEST_ENV", "max", {"max", "high"}) == "high"
    monkeypatch.delenv("X_TEST_ENV", raising=False)
    assert _validated_env("X_TEST_ENV", "max", {"max", "high"}) == "max"


def test_build_extra_payload_rejects_bad_thinking(monkeypatch):
    from alpha.config import _build_extra_payload
    monkeypatch.setenv("DEEPSEEK_THINKING", "enable")          # typo
    monkeypatch.setenv("DEEPSEEK_REASONING_EFFORT", "maximum")  # typo
    extras = _build_extra_payload("deepseek", "deepseek-v4-pro")
    assert extras["thinking"]["type"] == "enabled"             # default
    assert extras["reasoning_effort"] == "max"                 # default


# ── #D108: tool-call helpers tolerate missing id/name ──

def test_tc_helpers_tolerate_missing_fields():
    from alpha.executor import _tc_id, _tc_name
    assert _tc_id({}) == ""
    assert _tc_name({}) == ""
    assert _tc_id({"id": None}) == ""
    assert _tc_id({"id": "abc"}) == "abc"
    assert _tc_name({"name": "read_file"}) == "read_file"


# ── #D110: _atomic_write is crash-safe and writes content in full ──

def test_atomic_write_roundtrip_and_overwrite(tmp_path):
    from alpha.history import _atomic_write
    target = tmp_path / "s.json"
    _atomic_write(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}'
    # Overwrite with larger content — must fully replace, no leftover temps.
    big = '{"x": "' + ("y" * 100_000) + '"}'
    _atomic_write(target, big)
    assert target.read_text() == big
    assert mode_of(target) == 0o600
    # No orphan temp files left behind.
    assert list(tmp_path.glob(".s.json.*.tmp")) == []


def mode_of(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


# ── #D111: history readers degrade instead of crashing on OSError ──

def test_get_last_session_id_survives_ensure_dir_oserror(monkeypatch):
    import alpha.history as h
    monkeypatch.setattr(h, "_ensure_dir", lambda: (_ for _ in ()).throw(OSError("ro fs")))
    assert h.get_last_session_id() is None


def test_list_sessions_survives_ensure_dir_oserror(monkeypatch):
    import alpha.history as h
    monkeypatch.setattr(h, "_ensure_dir", lambda: (_ for _ in ()).throw(OSError("ro fs")))
    assert h.list_sessions() == []


# ── #D112: token-estimate cache still works (hit + invalidation) ──

def test_token_estimate_cache_hit_and_invalidation():
    from alpha.context import estimate_messages_tokens
    msgs = [{"role": "user", "content": "hello world"}]
    first = estimate_messages_tokens(msgs)
    assert estimate_messages_tokens(msgs) == first  # cache hit, same value
    msgs.append({"role": "assistant", "content": "a much longer reply " * 20})
    second = estimate_messages_tokens(msgs)
    assert second > first  # cache invalidated by len/tail change
