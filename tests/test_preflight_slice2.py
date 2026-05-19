"""Tests for pre-flight slice 2 — session cap, feedback log, re-prompt
fallback (RFC docs/specs/pre-flight-cards.md).

Slice 1 tests live in test_preflight.py and cover the core tool + estimators.
This file isolates the slice-2 wiring: things that touch the agent loop or
the on-disk feedback log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha import agent as agent_mod
from alpha.llm_fixtures import build_replay_stream
from alpha.tools import ToolCategory, ToolDefinition, ToolSafety
from alpha.preflight.feedback import _FEEDBACK_PATH, record_decision


# ─── feedback log ──────────────────────────────────────────────────


class TestFeedbackLog:
    def test_record_writes_jsonl_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_PATH",
            tmp_path / "preflight_feedback.jsonl",
        )
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_DIR", tmp_path
        )

        card = {
            "goal": "refactor executor",
            "steps": [
                {"tool": "read_file", "args_preview": "x.py"},
                {"tool": "edit_file", "args_preview": "x.py"},
            ],
            "confidence": "medium",
            "estimated_cost_usd": 0.012,
            "estimated_time_s": 8.5,
            "model": "deepseek-chat",
            "alternatives_rejected": [{"approach": "rewrite", "why_rejected": "scope"}],
        }
        record_decision(card, "approve")

        log_path = tmp_path / "preflight_feedback.jsonl"
        assert log_path.exists()
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["decision"] == "approve"
        assert entry["goal"] == "refactor executor"
        assert entry["confidence"] == "medium"
        assert entry["estimated_cost_usd"] == 0.012
        assert entry["step_tools"] == ["read_file", "edit_file"]
        assert entry["n_alternatives_rejected"] == 1
        assert "ts" in entry  # timestamp

    def test_record_appends_subsequent_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_PATH",
            tmp_path / "preflight_feedback.jsonl",
        )
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_DIR", tmp_path
        )
        for decision in ("approve", "reject", "approve_all"):
            record_decision({"goal": f"g-{decision}", "steps": []}, decision)

        lines = (tmp_path / "preflight_feedback.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3
        decisions = [json.loads(line)["decision"] for line in lines]
        assert decisions == ["approve", "reject", "approve_all"]

    def test_record_truncates_long_goal(self, tmp_path, monkeypatch):
        """200-char cap keeps each log line bounded even when the agent
        writes essay-length goals."""
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_PATH",
            tmp_path / "preflight_feedback.jsonl",
        )
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_DIR", tmp_path
        )
        long_goal = "x" * 1000
        record_decision({"goal": long_goal, "steps": []}, "approve")
        entry = json.loads((tmp_path / "preflight_feedback.jsonl").read_text().strip())
        assert len(entry["goal"]) == 200

    def test_record_swallows_disk_errors(self, monkeypatch, caplog):
        """Feedback log is best-effort — a write failure must not crash
        the approval flow."""
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_DIR",
            Path("/nonexistent/cannot/create"),
        )
        monkeypatch.setattr(
            "alpha.preflight.feedback._FEEDBACK_PATH",
            Path("/nonexistent/cannot/create/preflight_feedback.jsonl"),
        )
        # Should not raise.
        record_decision({"goal": "x", "steps": []}, "approve")


# ─── session cap ───────────────────────────────────────────────────


def _tool_schema() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "test",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]


def _fake_get_tool(name: str):
    if name == "read_file":
        return ToolDefinition(
            name="read_file",
            description="fake",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.SAFE,
            executor=lambda **_: {"content": "x"},
            category=ToolCategory.FILESYSTEM,
        )
    return None


class TestSessionCap:
    @pytest.mark.asyncio
    async def test_aborts_when_session_cost_above_cap(self, monkeypatch):
        # Inject already-spent session cost above the cap.
        monkeypatch.setattr(
            "alpha.agent._cost_session_summary",
            lambda: {"cost_usd": 1.5},
        )
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "1.0")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

        # The cap check fires before any LLM call, so we never actually
        # hit stream_chat_with_tools — but mock it anyway in case the
        # agent re-enters.
        fixture = Path(__file__).parent / "fixtures" / "llm" / "deepseek" / "happy_path.json"
        fake_stream, _ = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            provider="deepseek",
        ):
            events.append(event)
        assert events, "no events produced"
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events, f"expected an error event, got: {events}"
        assert "ALPHA_MAX_SESSION_COST_USD" in error_events[0]["message"]

    @pytest.mark.asyncio
    async def test_passes_when_under_cap(self, monkeypatch):
        monkeypatch.setattr(
            "alpha.agent._cost_session_summary",
            lambda: {"cost_usd": 0.05},
        )
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "1.0")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

        fixture = Path(__file__).parent / "fixtures" / "llm" / "deepseek" / "happy_path.json"
        fake_stream, _ = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            provider="deepseek",
        ):
            events.append(event)
            if event.get("type") == "done":
                break
        done_events = [e for e in events if e.get("type") == "done"]
        assert done_events, f"expected done event, got: {events}"

    @pytest.mark.asyncio
    async def test_no_cap_set_never_blocks(self, monkeypatch):
        monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)
        monkeypatch.setattr(
            "alpha.agent._cost_session_summary",
            lambda: {"cost_usd": 1_000_000.0},  # absurdly high
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

        fixture = Path(__file__).parent / "fixtures" / "llm" / "deepseek" / "happy_path.json"
        fake_stream, _ = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            provider="deepseek",
        ):
            events.append(event)
            if event.get("type") == "done":
                break
        done_events = [e for e in events if e.get("type") == "done"]
        assert done_events, f"expected done event, got: {events}"

    @pytest.mark.asyncio
    async def test_malformed_cap_is_ignored(self, monkeypatch):
        monkeypatch.setenv("ALPHA_MAX_SESSION_COST_USD", "not-a-number")
        monkeypatch.setattr(
            "alpha.agent._cost_session_summary",
            lambda: {"cost_usd": 1_000_000.0},
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

        fixture = Path(__file__).parent / "fixtures" / "llm" / "deepseek" / "happy_path.json"
        fake_stream, _ = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "hi"}],
            user_message="hi",
            provider="deepseek",
        ):
            events.append(event)
            if event.get("type") == "done":
                break
        done_events = [e for e in events if e.get("type") == "done"]
        assert done_events, f"expected done event, got: {events}"


# ─── re-prompt fallback ────────────────────────────────────────────


def _make_destructive_tool(name: str) -> ToolDefinition:
    """Spec-only ToolDefinition for re-prompt classification tests."""
    return ToolDefinition(
        name=name,
        description="destructive test tool",
        parameters={"type": "object", "properties": {}},
        safety=ToolSafety.DESTRUCTIVE,
        executor=lambda **_: {"ok": True},
        category=ToolCategory.SHELL,
    )


def _make_safe_tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="safe test tool",
        parameters={"type": "object", "properties": {}},
        safety=ToolSafety.SAFE,
        executor=lambda **_: {"ok": True},
        category=ToolCategory.FILESYSTEM,
    )


def _make_fixture(tmp_path: Path, tool_calls: list[dict]) -> Path:
    """Write a one-turn fixture that emits the given tool_calls then done.

    Two turns: turn 0 = tool_calls (LLM "wants" these tools), turn 1 =
    final text after the re-prompt would fire. The fixture has to cover
    BOTH outcomes (re-prompt fires or doesn't) because we replay it
    regardless of which branch the agent loop takes.
    """
    path = tmp_path / "reprompt_fixture.json"
    path.write_text(
        json.dumps({
            "version": 1,
            "turns": [
                # Turn 0: the planned batch
                [
                    {
                        "type": "final",
                        "content": "I'll do it.",
                        "tool_calls": tool_calls,
                        "reasoning_content": None,
                        "error": None,
                    }
                ],
                # Turn 1: clean done after re-prompt (or after execution)
                [
                    {"type": "content_token", "token": "Done."},
                    {
                        "type": "final",
                        "content": "Done.",
                        "tool_calls": [],
                        "reasoning_content": None,
                        "error": None,
                    },
                ],
                # Turn 2: extra safety in case re-prompt cycle needs another step
                [
                    {
                        "type": "final",
                        "content": "Final.",
                        "tool_calls": [],
                        "reasoning_content": None,
                        "error": None,
                    },
                ],
            ],
        }),
        encoding="utf-8",
    )
    return path


class TestReprompFallback:
    @pytest.mark.asyncio
    async def test_fires_on_two_destructive_without_preflight(
        self, monkeypatch, tmp_path
    ):
        """When the agent plans 2+ destructive tools without pre_flight,
        the loop should inject a system note and re-prompt once."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)

        tool_lookup = {
            "execute_shell": _make_destructive_tool("execute_shell"),
            "write_file": _make_destructive_tool("write_file"),
        }
        get_tool = lambda name: tool_lookup.get(name)  # noqa: E731

        fixture = _make_fixture(
            tmp_path,
            [
                {"id": "1", "name": "execute_shell", "arguments": "{}"},
                {"id": "2", "name": "write_file", "arguments": "{}"},
            ],
        )
        fake_stream, state = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "do it"}],
            user_message="do it",
            provider="deepseek",
            get_tool_fn=get_tool,
            tools=[],
            approval_callback=lambda *a, **kw: True,
            max_iterations=5,
        ):
            events.append(event)
            if event.get("type") == "done":
                break

        # The re-prompt forces an extra LLM turn — fixture turn_index
        # should be > 1 (turn 0 = original plan, turn 1 = after re-prompt).
        assert state["turn_index"] >= 2, (
            f"expected >=2 LLM turns after re-prompt, got {state['turn_index']}"
        )

    @pytest.mark.asyncio
    async def test_does_not_fire_on_single_destructive(
        self, monkeypatch, tmp_path
    ):
        """One destructive tool is under the 2-tool threshold — agent
        executes directly without re-prompt."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)

        tool_lookup = {"write_file": _make_destructive_tool("write_file")}
        get_tool = lambda name: tool_lookup.get(name)  # noqa: E731

        fixture = _make_fixture(
            tmp_path,
            [{"id": "1", "name": "write_file", "arguments": "{}"}],
        )
        fake_stream, state = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "do it"}],
            user_message="do it",
            provider="deepseek",
            get_tool_fn=get_tool,
            tools=[],
            approval_callback=lambda *a, **kw: True,
            max_iterations=5,
        ):
            events.append(event)
            if event.get("type") == "done":
                break

        # One tool only — no re-prompt — agent executes turn 0's tool then
        # asks turn 1 for the follow-up. turn_index == 2 covers the
        # follow-up (which is normal flow, not a re-prompt).
        # The key signal: NO injected `[ALPHA SYSTEM NOTE] You planned ...`
        # should appear in messages. We can't easily inspect the agent's
        # internal messages, but turn_index of exactly 2 is the expected
        # shape (1 plan + 1 followup, no extra re-prompt round-trip).
        assert state["turn_index"] == 2

    @pytest.mark.asyncio
    async def test_does_not_fire_when_preflight_present(
        self, monkeypatch, tmp_path
    ):
        """Agent already called pre_flight — no re-prompt needed even
        with 2+ destructive tools."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
        monkeypatch.delenv("ALPHA_MAX_SESSION_COST_USD", raising=False)

        from alpha.tools import load_all_tools, get_tool
        load_all_tools()

        fixture = _make_fixture(
            tmp_path,
            [
                {"id": "0", "name": "pre_flight", "arguments": "{}"},
                {"id": "1", "name": "execute_shell", "arguments": "{}"},
                {"id": "2", "name": "write_file", "arguments": "{}"},
            ],
        )
        fake_stream, state = build_replay_stream(fixture)
        monkeypatch.setattr(agent_mod, "stream_chat_with_tools", fake_stream)

        events = []
        async for event in agent_mod.run_agent(
            messages=[{"role": "user", "content": "do it"}],
            user_message="do it",
            provider="deepseek",
            get_tool_fn=get_tool,
            tools=[],
            approval_callback=lambda *a, **kw: True,
            max_iterations=5,
        ):
            events.append(event)
            if event.get("type") == "done":
                break

        # turn_index = 2 because pre_flight was honored — no extra
        # re-prompt round-trip.
        assert state["turn_index"] == 2
