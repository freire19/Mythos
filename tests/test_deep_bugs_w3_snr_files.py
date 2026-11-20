"""Regression tests for the DEEP_BUGS wave-3 SNR/file_tools fixes.

- #DB13: search_and_replace must not leak a `.sr_*` temp file when the
  atomic write fails with OSError.
- #DB15: empty search (SNR) / empty old_text (edit_file) must be rejected
  early instead of corrupting files via str.replace("", x).
- #DB18: glob_files with an absolute or empty pattern must return an
  actionable error dict instead of raising.
"""

from __future__ import annotations

import os
import shutil

import pytest

from alpha.tools._composite_snr import _search_and_replace
from alpha.tools.file_tools import _edit_file, _glob_files
from alpha.tools.workspace import AGENT_WORKSPACE


@pytest.fixture
def workspace_dir():
    target = AGENT_WORKSPACE / "test_deep_bugs_w3_sandbox"
    target.mkdir(exist_ok=True)
    yield target
    shutil.rmtree(target, ignore_errors=True)


class TestDB13TmpFileLeak:
    @pytest.mark.asyncio
    async def test_oserror_on_replace_removes_tmp_file(
        self, workspace_dir, monkeypatch
    ):
        # #DB13: os.replace failing (disk full / EPERM / cross-device) used to
        # `continue` without unlinking the `.sr_XXXXXX` temp file.
        target = workspace_dir / "victim.txt"
        target.write_text("foo bar foo\n")

        async def fake_run_tool(name, **kwargs):
            return {"results": [{"file": str(target)}]}

        monkeypatch.setattr(
            "alpha.tools._composite_snr._run_tool", fake_run_tool
        )
        monkeypatch.setattr(
            "alpha.tools._composite_snr._resolve_target",
            lambda p, **kw: (workspace_dir, None),
        )
        monkeypatch.setattr("alpha.checkpoints.snapshot", lambda *a, **kw: None)

        real_replace = os.replace

        def failing_replace(src, dst, *a, **kw):
            if str(dst) == str(target):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", failing_replace)

        result = await _search_and_replace(
            path=str(workspace_dir), search="foo", replace="baz", dry_run=False
        )

        assert result["files_changed"] == 0
        assert result["errors"] and result["errors"][0]["file"] == str(target)
        # Original intact, and no orphan temp file left behind.
        assert target.read_text() == "foo bar foo\n"
        leftovers = [f for f in os.listdir(workspace_dir) if f.startswith(".sr_")]
        assert leftovers == []


class TestDB15EmptySearch:
    @pytest.mark.asyncio
    async def test_snr_rejects_empty_search(self, workspace_dir):
        result = await _search_and_replace(
            path=str(workspace_dir), search="", replace="X", dry_run=False
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_edit_file_rejects_empty_old_text_replace_all(
        self, workspace_dir
    ):
        # #DB15: old_text="" + replace_all=True ran original.replace("", new)
        # → new_text inserted between every character.
        target = workspace_dir / "edit.txt"
        target.write_text("abc")
        result = await _edit_file(
            str(target), old_text="", new_text="X", replace_all=True
        )
        assert "error" in result
        assert target.read_text() == "abc"

    @pytest.mark.asyncio
    async def test_edit_file_rejects_empty_old_text_on_empty_file(
        self, workspace_dir
    ):
        # #DB15: on an empty file count("")==1 silently overwrote it.
        target = workspace_dir / "empty.txt"
        target.write_text("")
        result = await _edit_file(str(target), old_text="", new_text="X")
        assert "error" in result
        assert target.read_text() == ""


class TestDB18GlobPattern:
    @pytest.mark.asyncio
    async def test_absolute_pattern_returns_error(self, workspace_dir):
        # #DB18: NotImplementedError ("Non-relative patterns are unsupported")
        # used to escape on the first iteration.
        result = await _glob_files(
            pattern=str(workspace_dir / "**" / "*.py"), path=str(workspace_dir)
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_pattern_returns_error(self, workspace_dir):
        # #DB18: ValueError ("Unacceptable pattern") used to escape.
        result = await _glob_files(pattern="", path=str(workspace_dir))
        assert "error" in result

    @pytest.mark.asyncio
    async def test_valid_pattern_still_works(self, workspace_dir):
        (workspace_dir / "a.py").write_text("x = 1\n")
        result = await _glob_files(pattern="*.py", path=str(workspace_dir))
        assert "error" not in result
        assert result["count"] == 1
