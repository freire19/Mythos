"""End-to-end agent loop runs against fixture-replayed LLM streams.

Complements `test_agent_loop.py` (inline-tuple turns) by exercising the
same paths through versioned JSON fixtures — the format alpha.cost,
alpha.stats, and future record/replay tooling actually consume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import alpha.agent as agent_mod
from alpha import cost, stats
from alpha.llm_fixtures import build_replay_stream
from alpha.tools import ToolDefinition, ToolSafety


FIXTURE_ROOT = Path(__file__).parent.parent / "fixtures" / "llm"


@pytest.fixture(autouse=True)
def _reset_session():
    cost.reset_session()
    stats.reset_session()
    yield
    cost.reset_session()
    stats.reset_session()


@pytest.mark.asyncio
async def test_happy_path_replay_runs_to_completion(monkeypatch):
    """A 1-turn text fixture drives the agent to a clean done event."""
    fake_stream, state = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"
    )
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    events = []
    async for ev in agent_mod.run_agent(
        user_message="ping",
        messages=[{"role": "system", "content": "sys"}],
        provider="deepseek",
        get_tool_fn=lambda _: None,
        tools=[],
    ):
        events.append(ev)

    assert state["turn_index"] == 1
    assert any(e["type"] == "done" for e in events)
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert "".join(tokens) == "Olá! Posso ajudar?"


@pytest.mark.asyncio
async def test_replay_records_cost_and_stats(monkeypatch):
    """End-to-end smoke: fixture-replayed final event populates cost.usage."""
    fake_stream, _ = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"
    )
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    async for _ in agent_mod.run_agent(
        user_message="ping",
        messages=[{"role": "system", "content": "sys"}],
        provider="deepseek",
        get_tool_fn=lambda _: None,
        tools=[],
    ):
        pass

    s = cost.session_summary()
    assert s["calls"] == 1
    assert s["tokens_in"] == 24
    assert s["tokens_out"] == 6

    st = stats.session_summary()
    assert st["iterations"] >= 1


@pytest.mark.asyncio
async def test_tool_then_finish_replay(monkeypatch, tmp_path):
    """Two-turn fixture: model calls read_file, then produces final text."""
    target = tmp_path / "README.md"
    target.write_text("# project\n")

    async def _read_file(path: str, offset: int = 0, limit: int = 500):
        return {"path": str(path), "content": target.read_text()}

    read_tool = ToolDefinition(
        name="read_file",
        description="read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        safety=ToolSafety.SAFE,
        executor=_read_file,
        category="filesystem",
    )

    fake_stream, state = build_replay_stream(
        FIXTURE_ROOT / "openai" / "tool_then_finish.json"
    )
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    events = []
    async for ev in agent_mod.run_agent(
        user_message="describe README",
        messages=[{"role": "system", "content": "sys"}],
        provider="openai",
        get_tool_fn=lambda n: read_tool if n == "read_file" else None,
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    ):
        events.append(ev)

    assert state["turn_index"] == 2  # both fixture turns consumed
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert len(tool_results) == 1
    assert any(e["type"] == "done" for e in events)


@pytest.mark.asyncio
async def test_record_replay_round_trip(monkeypatch, tmp_path):
    """Run the agent against a fixture with ALPHA_RECORD_SESSION_PATH set,
    then load the recording and verify it has the same shape as the
    original fixture. End-to-end smoke for H2 #9 phase 1."""
    record_path = tmp_path / "session.fixture.json"
    monkeypatch.setenv("ALPHA_RECORD_SESSION_PATH", str(record_path))

    fake_stream, _ = build_replay_stream(
        FIXTURE_ROOT / "deepseek" / "happy_path.json"
    )
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    async for _ in agent_mod.run_agent(
        user_message="ping",
        messages=[{"role": "system", "content": "sys"}],
        provider="deepseek",
        get_tool_fn=lambda _: None,
        tools=[],
    ):
        pass

    assert record_path.exists(), "recording file should have been created"
    recorded = build_replay_stream(record_path)[0]  # rebuild replay from recording
    events = [
        e async for e in recorded([], [], 0.5, "deepseek")
    ]
    assert any(e["type"] == "final" for e in events)
    # The original fixture had 5 content_token events; the recording captures
    # exactly what the agent observed.
    tokens = [e for e in events if e["type"] == "content_token"]
    assert len(tokens) == 5


@pytest.mark.asyncio
async def test_error_finish_reason_surfaces(monkeypatch):
    """When the LLM stream ends with `error` set, the agent must propagate
    it as an `error` event with the message attached — not a silent empty
    turn. Regression guard for the malformed-tool-call class of provider
    failure (Gemini's MALFORMED_FUNCTION_CALL is the canonical example)."""
    fake_stream, _ = build_replay_stream(
        FIXTURE_ROOT / "anthropic" / "empty_finish_reason.json"
    )
    monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

    events = []
    async for ev in agent_mod.run_agent(
        user_message="hi",
        messages=[{"role": "system", "content": "sys"}],
        provider="anthropic",
        get_tool_fn=lambda _: None,
        tools=[],
    ):
        events.append(ev)

    err = next((e for e in events if e["type"] == "error"), None)
    assert err is not None, f"expected an error event, got {[e['type'] for e in events]}"
    assert "malformed_tool_call" in err["message"]
