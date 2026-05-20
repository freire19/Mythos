"""Tests for delegate_consensus — Plano-Upgrade-v3 H2 #8."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alpha.tools.delegate_tools import _cluster_answers, _delegate_consensus


# ─── _cluster_answers (pure) ──────────────────────────────────────


class TestClusterAnswers:
    def test_all_identical(self):
        answers = ["yes, it's a bug", "yes, it's a bug", "yes, it's a bug"]
        clusters = _cluster_answers(answers)
        assert len(clusters) == 1
        assert sorted(clusters[0]) == [0, 1, 2]

    def test_all_different(self):
        # Three substantively different verdicts — should each be their
        # own cluster. Strings must be far enough apart that
        # SequenceMatcher ratio < 0.7 (the threshold). Single-word swaps
        # in a long template cluster trivially; use different content.
        answers = [
            "yes this is a critical security bug, sanitize the input",
            "no the code is fine, the test case is wrong",
            "it depends on whether you control the caller side",
        ]
        clusters = _cluster_answers(answers)
        assert len(clusters) == 3

    def test_majority_with_dissent(self):
        answers = [
            "The function has a null-pointer bug on line 42.",
            "The function has a null-pointer bug on line 42.",
            "I see no bug here — the code is correct.",
        ]
        clusters = _cluster_answers(answers)
        clusters.sort(key=lambda c: -len(c))
        assert len(clusters[0]) == 2
        assert len(clusters[1]) == 1

    def test_near_duplicates_cluster(self):
        # Same conclusion, slightly different wording — should cluster.
        answers = [
            "The function returns None on empty input, which is a bug.",
            "The function returns None on empty input, which is a bug.",
            "The function returns None on empty input — that is a bug.",
        ]
        clusters = _cluster_answers(answers)
        # Threshold is 0.7; near-identical strings exceed that.
        assert len(clusters) == 1

    def test_empty_answers_share_single_cluster(self):
        # Empty/whitespace answers share one degenerate cluster so the
        # consensus output doesn't fan out into N "failed" groups.
        answers = ["valid answer", "", "  ", "valid answer"]
        clusters = _cluster_answers(answers)
        sizes = sorted(len(c) for c in clusters)
        assert sizes == [2, 2]

    def test_empty_answers_dont_absorb_valid(self):
        answers = ["", "real answer", "", "another real answer"]
        clusters = _cluster_answers(answers)
        empties = next(c for c in clusters if 0 in c)
        assert 1 not in empties
        assert 3 not in empties
        assert sorted(empties) == [0, 2]


# ─── _delegate_consensus integration ──────────────────────────────


@pytest.mark.asyncio
async def test_invalid_n():
    out = await _delegate_consensus("q", n=1)
    assert out["ok"] is False
    assert out["category"] == "invalid_args"
    assert ">= 2" in out["error"]


@pytest.mark.asyncio
async def test_n_above_cap(monkeypatch):
    from alpha.config import FEATURES
    monkeypatch.setitem(FEATURES, "max_parallel_agents", 3)
    out = await _delegate_consensus("q", n=10)
    assert out["ok"] is False
    assert "exceeds max_parallel_agents" in out["error"]


@pytest.mark.asyncio
async def test_feature_disabled(monkeypatch):
    from alpha.config import FEATURES
    monkeypatch.setitem(FEATURES, "multi_agent_enabled", False)
    out = await _delegate_consensus("q", n=3)
    assert out["ok"] is False
    assert out["category"] == "feature_disabled"


def _fake_subagent(*answers: str):
    """Build an async patch target that returns each scripted answer in order."""
    call = {"i": 0}

    async def fake(*args, **kwargs):
        idx = call["i"]
        call["i"] += 1
        return {
            "status": "completed",
            "result": answers[idx % len(answers)],
            "tools_used": [],
            "iterations": 0,
            "agent_id": f"a{idx}",
            "scratch_dir": "/tmp",
            "scratch_files": [],
        }

    return fake


@pytest.mark.asyncio
async def test_consensus_reached(monkeypatch):
    fake = _fake_subagent(
        "yes, line 42 is a null-deref bug",
        "yes, line 42 is a null-deref bug",
        "no bug, the code is fine",
    )
    monkeypatch.setattr("alpha.tools.delegate_tools._run_subagent", fake)

    out = await _delegate_consensus("is there a bug?", n=3)
    assert out["ok"] is True
    assert out["n_successful"] == 3
    assert out["consensus"]["reached"] is True
    assert out["consensus"]["majority"]["size"] == 2
    assert set(out["consensus"]["majority"]["agents"]) == {1, 2}
    assert len(out["consensus"]["dissent"]) == 1
    assert out["consensus"]["dissent"][0]["agents"] == [3]


@pytest.mark.asyncio
async def test_unanimous(monkeypatch):
    fake = _fake_subagent("same answer", "same answer", "same answer")
    monkeypatch.setattr("alpha.tools.delegate_tools._run_subagent", fake)
    out = await _delegate_consensus("q", n=3)
    assert out["consensus"]["reached"] is True
    assert out["consensus"]["majority"]["size"] == 3
    assert out["consensus"]["dissent"] == []


@pytest.mark.asyncio
async def test_no_majority(monkeypatch):
    # 3 substantively distinct verdicts → no strict majority.
    fake = _fake_subagent(
        "yes this is a critical security bug, sanitize the input",
        "no the code is fine, the test case is wrong",
        "it depends on whether you control the caller side",
    )
    monkeypatch.setattr("alpha.tools.delegate_tools._run_subagent", fake)
    out = await _delegate_consensus("q", n=3)
    assert out["ok"] is True
    # Largest cluster is 1, which is not > n//2 (=1), so consensus_reached=False.
    assert out["consensus"]["reached"] is False
    assert out["consensus"]["majority"]["size"] == 1
    assert len(out["consensus"]["dissent"]) == 2


@pytest.mark.asyncio
async def test_failed_agent_excluded(monkeypatch):
    async def fake(*args, **kwargs):
        # First two succeed with same answer, third raises.
        idx = kwargs.get("label", "#0")
        if "#3" in str(idx) or "#3" == idx:
            raise RuntimeError("provider down")
        return {
            "status": "completed",
            "result": "the consensus answer",
            "tools_used": [], "iterations": 0,
            "agent_id": "a", "scratch_dir": "/tmp", "scratch_files": [],
        }

    monkeypatch.setattr("alpha.tools.delegate_tools._run_subagent", fake)
    out = await _delegate_consensus("q", n=3)
    assert out["ok"] is True
    assert out["n_successful"] == 2
    assert out["n_failed"] == 1
    # Failed agent shows up in `answers` with the error
    failed = [a for a in out["answers"] if a["status"] == "failed"]
    assert len(failed) == 1
    assert "provider down" in failed[0]["error"]


@pytest.mark.asyncio
async def test_all_failed(monkeypatch):
    async def fake(*args, **kwargs):
        raise RuntimeError("network down")
    monkeypatch.setattr("alpha.tools.delegate_tools._run_subagent", fake)
    out = await _delegate_consensus("q", n=3)
    assert out["ok"] is False
    assert out["category"] == "all_failed"
    assert out["failed"] == 3
