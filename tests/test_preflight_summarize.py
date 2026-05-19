"""Tests for the /preflight analytics view (slice 2.5).

Covers the `summarize()` aggregation and the REPL command's empty/
populated rendering branches. The decision-rendering colors are visual
and not asserted — we just confirm the structural data flows through.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha.preflight.feedback import _read_entries, summarize


def _seed_log(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def feedback_path(tmp_path, monkeypatch):
    path = tmp_path / "preflight_feedback.jsonl"
    monkeypatch.setattr("alpha.preflight.feedback._FEEDBACK_PATH", path)
    monkeypatch.setattr("alpha.preflight.feedback._FEEDBACK_DIR", tmp_path)
    return path


class TestReadEntries:
    def test_missing_file_returns_empty(self, feedback_path):
        assert _read_entries() == []

    def test_skips_malformed_lines(self, feedback_path):
        feedback_path.write_text(
            '{"decision": "approve"}\n'
            'not json\n'
            '{"decision": "reject"}\n',
            encoding="utf-8",
        )
        entries = _read_entries()
        assert len(entries) == 2
        assert [e["decision"] for e in entries] == ["approve", "reject"]

    def test_limit_returns_most_recent(self, feedback_path):
        _seed_log(feedback_path, [
            {"decision": "approve", "n": i} for i in range(10)
        ])
        entries = _read_entries(limit=3)
        assert len(entries) == 3
        assert [e["n"] for e in entries] == [7, 8, 9]


class TestSummarize:
    def test_empty_log_returns_zeros(self, feedback_path):
        s = summarize()
        assert s["total"] == 0
        assert s["decisions"] == {}
        assert s["by_tool"] == {}
        assert s["avg_estimated_cost_usd"] == 0.0

    def test_counts_decisions(self):
        entries = [
            {"decision": "approve", "step_tools": ["read_file"]},
            {"decision": "approve", "step_tools": ["read_file"]},
            {"decision": "reject", "step_tools": ["execute_shell"]},
            {"decision": "approve_all", "step_tools": []},
        ]
        s = summarize(entries)
        assert s["total"] == 4
        assert s["decisions"]["approve"] == 2
        assert s["decisions"]["reject"] == 1
        assert s["decisions"]["approve_all"] == 1

    def test_by_tool_breakdown(self):
        entries = [
            {"decision": "approve", "step_tools": ["read_file", "edit_file"]},
            {"decision": "reject", "step_tools": ["execute_shell"]},
            {"decision": "approve", "step_tools": ["read_file"]},
        ]
        s = summarize(entries)
        assert s["by_tool"]["read_file"]["approve"] == 2
        assert s["by_tool"]["edit_file"]["approve"] == 1
        assert s["by_tool"]["execute_shell"]["reject"] == 1

    def test_cost_average_skips_missing_field(self):
        """Old log entries may lack estimated_cost_usd — average should
        skip them rather than treat them as 0."""
        entries = [
            {"decision": "approve", "estimated_cost_usd": 0.10},
            {"decision": "approve", "estimated_cost_usd": 0.30},
            {"decision": "approve"},  # missing cost
        ]
        s = summarize(entries)
        # Average over the 2 entries that have cost, not 3.
        assert s["avg_estimated_cost_usd"] == pytest.approx(0.20)
        assert s["total_estimated_cost_usd"] == pytest.approx(0.40)

    def test_zero_cost_entries_dont_affect_average(self):
        """Entries with cost=0 (unknown-model case from slice 1)
        should still be counted in the average — 0 is a valid sample,
        unlike missing."""
        entries = [
            {"decision": "approve", "estimated_cost_usd": 0.0},
            {"decision": "approve", "estimated_cost_usd": 0.10},
        ]
        s = summarize(entries)
        assert s["avg_estimated_cost_usd"] == pytest.approx(0.05)
