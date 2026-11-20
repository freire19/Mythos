"""Regression tests for DEEP_BUGS wave 3 — pipeline_tools.

- #DB11: single-cmd sem redirect de stdin nao pode herdar o terminal do
  REPL (stdin=None) — default deve ser DEVNULL.
- #DB21: stdout e stdout_append (ou stderr/stderr_append) no mesmo
  segmento faziam double-open e vazavam o primeiro fd — agora o parse
  rejeita chaves conflitantes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from alpha.tools import pipeline_tools


def _patch_workspace(monkeypatch, ws: Path):
    from alpha.tools import path_helpers, workspace as ws_module

    monkeypatch.setattr(pipeline_tools, "AGENT_WORKSPACE", ws)
    monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", ws)
    monkeypatch.setattr(path_helpers, "AGENT_WORKSPACE", ws)


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.pid = 12345

    async def communicate(self, input=None):
        return b"", b""

    async def wait(self):
        return 0


class TestDB11StdinDefault:
    def _capture_exec(self, monkeypatch):
        captured = {}

        async def fake_exec(*parts, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(
            pipeline_tools.asyncio, "create_subprocess_exec", fake_exec
        )
        return captured

    def test_single_cmd_without_redirect_uses_devnull(
        self, tmp_path, monkeypatch
    ):
        _patch_workspace(monkeypatch, tmp_path)
        captured = self._capture_exec(monkeypatch)

        asyncio.run(
            pipeline_tools._execute_pipe_chain(
                ["cat"], str(tmp_path), {}, timeout=5
            )
        )
        assert captured["stdin"] == asyncio.subprocess.DEVNULL

    def test_single_cmd_with_stdin_redirect_uses_file_handle(
        self, tmp_path, monkeypatch
    ):
        _patch_workspace(monkeypatch, tmp_path)
        captured = self._capture_exec(monkeypatch)

        infile = tmp_path / "in.txt"
        infile.write_text("hello\n")

        asyncio.run(
            pipeline_tools._execute_pipe_chain(
                [f"cat < {infile}"], str(tmp_path), {}, timeout=5
            )
        )
        assert hasattr(captured["stdin"], "read")

    def test_multi_cmd_chain_still_uses_pipe(self, tmp_path, monkeypatch):
        _patch_workspace(monkeypatch, tmp_path)
        captured = self._capture_exec(monkeypatch)

        asyncio.run(
            pipeline_tools._execute_pipe_chain(
                ["echo hi", "cat"], str(tmp_path), {}, timeout=5
            )
        )
        assert captured["stdin"] == asyncio.subprocess.PIPE


class TestDB21ConflictingRedirects:
    def test_stdout_and_append_same_segment_rejected(self):
        with pytest.raises(ValueError, match="conflitantes"):
            pipeline_tools._parse_segment("echo hi > a.txt >> b.txt")

    def test_stderr_and_append_same_segment_rejected(self):
        with pytest.raises(ValueError, match="conflitantes"):
            pipeline_tools._parse_segment("echo hi 2> a.txt 2>> b.txt")

    def test_non_conflicting_redirects_still_parse(self):
        parts, redirects = pipeline_tools._parse_segment(
            "echo hi > out.txt 2>> err.txt"
        )
        assert parts == ["echo", "hi"]
        assert redirects == {"stdout": "out.txt", "stderr_append": "err.txt"}

    def test_execute_pipeline_blocks_conflicting_redirects(
        self, tmp_path, monkeypatch
    ):
        _patch_workspace(monkeypatch, tmp_path)

        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        result = asyncio.run(
            pipeline_tools._execute_pipeline(f"echo hi > {a} >> {b}")
        )
        assert result.get("blocked") is True
        assert "conflitantes" in result["error"]
        # O primeiro target nao deve nem ter sido criado/truncado.
        assert not a.exists()
