"""Tests for `alpha.replay` — session replay CLI (H2 #9 phase 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alpha.replay import (
    extract_assistant_replies,
    extract_user_turns,
    main,
    replay_session,
)


class TestExtractUserTurns:
    def test_single_string_content(self):
        msgs = [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "second"},
        ]
        assert extract_user_turns(msgs) == ["hello", "second"]

    def test_multimodal_user_content(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": "..."}},
                {"type": "text", "text": "in detail"},
            ],
        }]
        assert extract_user_turns(msgs) == ["describe this\nin detail"]

    def test_skips_non_user_roles(self):
        msgs = [
            {"role": "assistant", "content": "a"},
            {"role": "tool", "content": "t"},
            {"role": "system", "content": "s"},
        ]
        assert extract_user_turns(msgs) == []


class TestExtractAssistantReplies:
    def test_one_turn(self):
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
        assert extract_assistant_replies(msgs) == ["a"]

    def test_two_turns(self):
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        assert extract_assistant_replies(msgs) == ["a1", "a2"]

    def test_skips_tool_only_assistant(self):
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "final reply"},
        ]
        assert extract_assistant_replies(msgs) == ["final reply"]

    def test_empty_reply_keeps_alignment(self):
        # Turn 1 had only a tool call (no plain reply). The list still
        # has one entry so it aligns with user_turns indexing.
        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x"}]},
            {"role": "tool", "content": "r"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "answer 2"},
        ]
        replies = extract_assistant_replies(msgs)
        assert len(replies) == 2
        assert replies[0] == ""  # turn 1 had no plain text reply
        assert replies[1] == "answer 2"


@pytest.mark.asyncio
async def test_replay_session_not_found(monkeypatch):
    monkeypatch.setattr("alpha.replay.load_session", lambda _: None)
    out = await replay_session("nope", provider="deepseek")
    assert out["ok"] is False
    assert "not found" in out["error"]


@pytest.mark.asyncio
async def test_replay_session_no_user_turns(monkeypatch):
    monkeypatch.setattr("alpha.replay.load_session", lambda _: [
        {"role": "system", "content": "sys"},
    ])
    out = await replay_session("empty", provider="deepseek")
    assert out["ok"] is False
    assert "no user turns" in out["error"]


@pytest.mark.asyncio
async def test_replay_session_drives_agent(monkeypatch):
    """End-to-end: with run_agent mocked, replay_session walks turns and
    accumulates results."""
    saved = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "original answer 1"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "original answer 2"},
    ]
    monkeypatch.setattr("alpha.replay.load_session", lambda _: saved)
    monkeypatch.setattr("alpha.replay.get_openai_tools", lambda: [])

    call = {"i": 0}

    async def fake_run_agent(**kwargs):
        call["i"] += 1
        yield {"type": "token", "text": f"new-{call['i']}"}
        yield {"type": "done", "reply": f"new answer {call['i']}"}

    monkeypatch.setattr("alpha.replay.run_agent", fake_run_agent)

    out = await replay_session("any", provider="anthropic")
    assert out["ok"] is True
    assert out["n_turns"] == 2
    assert out["turns"][0]["original"] == "original answer 1"
    assert out["turns"][0]["replay"] == "new answer 1"
    assert out["turns"][1]["replay"] == "new answer 2"


def test_main_session_not_found(monkeypatch, capsys):
    monkeypatch.setattr("alpha.replay.load_session", lambda _: None)
    rc = main(["does-not-exist"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "session not found" in captured.err
