"""Tests for cross-session memory (H2 #10)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from alpha import memory


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """Redirect memory storage to tmp_path so tests never touch real ~/.alpha.

    Monkeypatches the module's `_memory_root` accessor so the test doesn't
    need to mirror the internal path-construction (which uses
    `alpha.settings.alpha_user_dir`)."""
    monkeypatch.setattr("alpha.memory._memory_root", lambda: tmp_path / "memory")
    yield


class TestRecord:
    def test_basic_record(self):
        out = memory.record("user prefers Portuguese replies", scope="global")
        assert out["ok"] is True
        assert "global.md" in out["path"]
        path = Path(out["path"])
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "user prefers Portuguese replies" in content
        assert "## " in content  # has a header

    def test_default_scope_is_workspace(self):
        out = memory.record("project uses pytest")
        assert "workspace-" in out["path"]

    def test_kind_sanitization(self):
        out = memory.record("note A", kind="pref/erence!", scope="global")
        # Non-alpha chars stripped from kind
        assert out["kind"] == "preference"
        path = Path(out["path"])
        assert "(preference)" in path.read_text()

    def test_kind_empty_becomes_note(self):
        out = memory.record("hi", kind="!!!", scope="global")
        assert out["kind"] == "note"

    def test_invalid_scope(self):
        out = memory.record("x", scope="nonexistent")
        assert out["ok"] is False

    def test_empty_content(self):
        assert memory.record("", scope="global")["ok"] is False
        assert memory.record("   ", scope="global")["ok"] is False

    def test_trims_when_over_cap(self, monkeypatch):
        monkeypatch.setattr(memory, "MAX_MEMORY_BYTES", 300)
        # Each entry's body is ~100 bytes; 5 entries forces trim past 300.
        body = "x" * 100
        for i in range(5):
            memory.record(f"{body} marker{i}", scope="global",
                          now=datetime(2026, 1, 1, 0, i))
        path = memory._path_for("global")
        content = path.read_text()
        # Newest entry must survive
        assert "marker4" in content
        # The oldest entry must have been dropped
        assert "marker0" not in content


class TestListEntries:
    def test_empty_returns_empty_list(self):
        assert memory.list_entries(scope="global") == []

    def test_newest_first(self):
        memory.record("first", scope="global",
                      now=datetime(2026, 1, 1, 10, 0))
        memory.record("second", scope="global",
                      now=datetime(2026, 1, 2, 10, 0))
        memory.record("third", scope="global",
                      now=datetime(2026, 1, 3, 10, 0))
        entries = memory.list_entries(scope="global")
        assert len(entries) == 3
        assert entries[0]["body"] == "third"
        assert entries[2]["body"] == "first"

    def test_legacy_no_headers(self, monkeypatch):
        path = memory._path_for("global")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("free-form text with no markdown headers")
        entries = memory.list_entries(scope="global")
        assert len(entries) == 1
        assert entries[0]["kind"] == "raw"


class TestForget:
    def test_forget_by_index(self):
        memory.record("a", scope="global", now=datetime(2026, 1, 1, 0, 0))
        memory.record("b", scope="global", now=datetime(2026, 1, 2, 0, 0))
        memory.record("c", scope="global", now=datetime(2026, 1, 3, 0, 0))
        out = memory.forget(1, scope="global")  # newest = "c"
        assert out["ok"] is True
        assert out["removed"]["body"] == "c"
        remaining = memory.list_entries(scope="global")
        assert [e["body"] for e in remaining] == ["b", "a"]

    def test_forget_out_of_range(self):
        memory.record("a", scope="global")
        assert memory.forget(99, scope="global")["ok"] is False

    def test_forget_empty(self):
        assert memory.forget(1, scope="global")["ok"] is False


class TestClear:
    def test_clear_removes_all(self):
        memory.record("x", scope="global")
        memory.record("y", scope="global")
        out = memory.clear(scope="global")
        assert out["removed_count"] == 2
        assert memory.list_entries(scope="global") == []


class TestSummaryForPrompt:
    def test_empty(self):
        assert memory.summary_for_prompt() == ""

    def test_combines_global_and_workspace(self):
        memory.record("user style", scope="global",
                      now=datetime(2026, 1, 1, 0, 0))
        memory.record("project quirk", scope="workspace",
                      now=datetime(2026, 1, 2, 0, 0))
        out = memory.summary_for_prompt()
        assert "Global memory:" in out
        assert "This workspace:" in out
        assert "user style" in out
        assert "project quirk" in out

    def test_truncation(self):
        for i in range(50):
            memory.record(f"some long entry number {i} with padding text " * 5,
                          scope="global",
                          now=datetime(2026, 1, 1, 0, i % 60))
        out = memory.summary_for_prompt(max_chars=500)
        assert len(out) <= 500
        assert out.endswith("...")


class TestWorkspaceToken:
    def test_basename_from_cwd(self, monkeypatch, tmp_path):
        proj = tmp_path / "MyProject"
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert memory._workspace_token() == "MyProject"

    def test_sanitizes_special_chars(self, monkeypatch, tmp_path):
        weird = tmp_path / "with spaces & symbols!"
        weird.mkdir()
        monkeypatch.chdir(weird)
        token = memory._workspace_token()
        assert all(c.isalnum() or c in "_.-" for c in token)

    def test_fallback_when_root(self, monkeypatch):
        monkeypatch.chdir("/")
        token = memory._workspace_token()
        # basename("/") == "" — falls back to "default"
        assert token == "default"
