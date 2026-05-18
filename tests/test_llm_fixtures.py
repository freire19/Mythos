"""Tests for alpha.llm_fixtures — record/replay infrastructure."""

import json
from pathlib import Path

import pytest

from alpha.llm_fixtures import (
    FIXTURE_VERSION,
    FixtureError,
    build_replay_stream,
    load_fixture,
    record_wrap,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "llm"


# ─── load_fixture ──────────────────────────────────────────────────


def test_load_happy_path():
    data = load_fixture(FIXTURE_ROOT / "deepseek" / "happy_path.json")
    assert data["version"] == FIXTURE_VERSION
    assert data["provider"] == "deepseek"
    assert len(data["turns"]) == 1
    assert any(e["type"] == "final" for e in data["turns"][0])


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FixtureError, match="failed to read"):
        load_fixture(tmp_path / "nope.json")


def test_load_wrong_version_raises(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"version": 999, "turns": [[{"type": "final"}]]}))
    with pytest.raises(FixtureError, match="version="):
        load_fixture(p)


def test_load_no_turns_raises(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"version": 1, "turns": []}))
    with pytest.raises(FixtureError, match="empty or missing"):
        load_fixture(p)


def test_load_turn_without_final_raises(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({
        "version": 1,
        "turns": [[{"type": "content_token", "token": "x"}]],
    }))
    with pytest.raises(FixtureError, match="no 'final' event"):
        load_fixture(p)


# ─── build_replay_stream ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_yields_events_in_order():
    fake_stream, state = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"
    )
    events = []
    async for ev in fake_stream([], [], 0.5, "deepseek"):
        events.append(ev)
    assert state["turn_index"] == 1
    assert len(state["calls"]) == 1
    assert state["calls"][0]["provider"] == "deepseek"
    # Token events first, then final
    assert events[0]["type"] == "content_token"
    assert events[-1]["type"] == "final"
    assert events[-1]["content"] == "Olá! Posso ajudar?"


@pytest.mark.asyncio
async def test_replay_serves_turns_sequentially():
    fake_stream, state = build_replay_stream(
        FIXTURE_ROOT / "openai" / "tool_then_finish.json"
    )

    # First call → tool_call turn
    events1 = [e async for e in fake_stream([{}], [], 0.5, "openai")]
    final1 = next(e for e in events1 if e["type"] == "final")
    assert len(final1["tool_calls"]) == 1
    assert final1["tool_calls"][0]["name"] == "read_file"

    # Second call → text response turn
    events2 = [e async for e in fake_stream([{}, {}], [], 0.5, "openai")]
    final2 = next(e for e in events2 if e["type"] == "final")
    assert "README" in final2["content"]
    assert state["turn_index"] == 2


@pytest.mark.asyncio
async def test_replay_exhaustion_raises():
    fake_stream, state = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"  # 1-turn fixture
    )
    # First call OK
    [e async for e in fake_stream([], [], 0.5, "deepseek")]
    # Second call — agent shouldn't ask, but if it does we yell loud
    with pytest.raises(AssertionError, match="exhausted"):
        async for _ in fake_stream([], [], 0.5, "deepseek"):
            pass
    assert state["exhausted"] is True


@pytest.mark.asyncio
async def test_replay_yielded_dict_is_a_copy():
    """Mutating an event the test received must not corrupt the fixture
    for the next call."""
    fake_stream, _ = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"
    )
    events1 = [e async for e in fake_stream([], [], 0.5, "deepseek")]
    events1[-1]["content"] = "MUTATED"
    # Reload and check the fixture wasn't touched
    raw = load_fixture(FIXTURE_ROOT / "deepseek" / "happy_path.json")
    assert raw["turns"][0][-1]["content"] == "Olá! Posso ajudar?"


@pytest.mark.asyncio
async def test_replay_with_delay_actually_awaits(monkeypatch):
    """delay_s=N should await asyncio.sleep(N) between events."""
    sleeps: list[float] = []
    import asyncio as _asyncio
    original = _asyncio.sleep

    async def fake_sleep(d):
        sleeps.append(d)
        await original(0)

    monkeypatch.setattr("alpha.llm_fixtures.asyncio.sleep", fake_sleep)
    fake_stream, _ = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json",
        delay_s=0.05,
    )
    async for _ in fake_stream([], [], 0.5, "deepseek"):
        pass
    # 5 tokens + 1 final = 6 events, all should have triggered a sleep
    assert sleeps == [0.05] * 6


# ─── record_wrap ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_captures_events(tmp_path):
    async def source():
        yield {"type": "content_token", "token": "hi"}
        yield {"type": "final", "content": "hi", "tool_calls": [], "error": None}

    sink = tmp_path / "recorded.json"
    out = []
    async for ev in record_wrap(source(), sink, scenario="t", provider="p", model="m"):
        out.append(ev)

    assert sink.exists()
    data = json.loads(sink.read_text())
    assert data["version"] == FIXTURE_VERSION
    assert data["scenario"] == "t"
    assert data["provider"] == "p"
    assert data["model"] == "m"
    assert len(data["turns"]) == 1
    assert data["turns"][0][-1]["type"] == "final"


# ─── append_turn (session recording) ──────────────────────────────


def test_append_turn_creates_file(tmp_path):
    from alpha.llm_fixtures import append_turn, load_fixture
    p = tmp_path / "sess.json"
    append_turn(
        p,
        [
            {"type": "content_token", "token": "hi"},
            {"type": "final", "content": "hi", "tool_calls": [], "error": None},
        ],
        scenario="t", provider="p", model="m",
    )
    data = load_fixture(p)
    assert data["scenario"] == "t"
    assert data["provider"] == "p"
    assert len(data["turns"]) == 1
    assert data["turns"][0][-1]["type"] == "final"


def test_append_turn_appends_to_existing(tmp_path):
    from alpha.llm_fixtures import append_turn, load_fixture
    p = tmp_path / "sess.json"
    append_turn(p, [
        {"type": "final", "content": "a", "tool_calls": [], "error": None},
    ], scenario="s", provider="p", model="m")
    append_turn(p, [
        {"type": "final", "content": "b", "tool_calls": [], "error": None},
    ], scenario="ignored-on-append", provider="p", model="m")
    data = load_fixture(p)
    assert len(data["turns"]) == 2
    assert data["scenario"] == "s"  # initial scenario preserved across appends


def test_append_turn_skips_empty_events(tmp_path):
    from alpha.llm_fixtures import append_turn
    p = tmp_path / "sess.json"
    append_turn(p, [], scenario="t", provider="p", model="m")
    assert not p.exists()


def test_append_turn_recovers_from_corrupt_file(tmp_path):
    from alpha.llm_fixtures import append_turn, load_fixture
    p = tmp_path / "sess.json"
    p.write_text("{not valid json")
    append_turn(p, [
        {"type": "final", "content": "x", "tool_calls": [], "error": None},
    ], scenario="recovered", provider="p", model="m")
    data = load_fixture(p)
    assert data["scenario"] == "recovered"
    assert len(data["turns"]) == 1


@pytest.mark.asyncio
async def test_record_drops_non_serializable_fields(tmp_path):
    class _Weird:
        def __repr__(self): return "<Weird>"

    async def source():
        yield {"type": "final", "content": "x", "tool_calls": [],
               "error": None, "_internal": _Weird()}

    sink = tmp_path / "weird.json"
    async for _ in record_wrap(source(), sink):
        pass
    data = json.loads(sink.read_text())
    assert data["turns"][0][0]["_internal"] == "<Weird>"
