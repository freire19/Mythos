"""Regression tests for the DEEP_SECURITY 'deferred' BAIXOs that were then
resolved on request: #010 (redirect quoting), #015 (_run_tool workspace),
#D143 (audio external-upload confinement)."""
from __future__ import annotations

import pytest


# ── #010: redirect validation is quoting-aware ──

def test_redirect_quoted_gt_not_flagged_real_caught():
    from alpha.tools.pipeline_tools import _validate_redirect_paths as v
    # `>` inside a quoted arg is NOT a redirect → no false-positive rejection.
    assert v('echo "a > /etc/x" > out.txt') is None
    assert v('echo "a > b"') is None
    # real redirects outside the workspace (spaced AND attached) are caught.
    assert v("cat x > /etc/passwd") is not None
    assert v("cat x >/etc/passwd") is not None
    # fd-dup and in-workspace targets are fine.
    assert v("cmd 2>&1 | grep x") is None
    assert v("echo hi >> out.txt") is None


# ── #015: composite _run_tool re-enforces the active workspace ──

class _FakeTool:
    def __init__(self, ran_flag):
        self._ran = ran_flag

    async def executor(self, **kw):
        self._ran["ran"] = True
        return {"ok": True}


@pytest.mark.asyncio
async def test_run_tool_enforces_active_workspace(tmp_path, monkeypatch):
    from alpha.tools import _composite_helpers as ch
    from alpha.agents.workspace import active_workspace

    ran = {"ran": False}
    monkeypatch.setattr(ch, "get_tool", lambda name: _FakeTool(ran))

    # read_file is in PATH_PARAMS_BY_TOOL — with a workspace active, an
    # out-of-workspace path arg must be rejected BEFORE the executor runs.
    token = active_workspace.set(str(tmp_path))
    try:
        out = await ch._run_tool("read_file", path="/etc/passwd")
    finally:
        active_workspace.reset(token)
    assert out.get("category") == "violation", out
    assert ran["ran"] is False  # executor never reached


@pytest.mark.asyncio
async def test_run_tool_no_workspace_skips_enforcement(monkeypatch):
    from alpha.tools import _composite_helpers as ch
    from alpha.agents.workspace import active_workspace

    ran = {"ran": False}
    monkeypatch.setattr(ch, "get_tool", lambda name: _FakeTool(ran))
    assert active_workspace.get() is None  # main-agent: no workspace gate
    out = await ch._run_tool("read_file", path="/etc/passwd")
    assert out.get("category") != "violation"
    assert ran["ran"] is True  # executor ran (no enforcement)


# ── #D143: /audio confines the path (external Whisper upload) ──

def test_audio_rejects_outside_workspace(monkeypatch, tmp_path):
    from alpha.cli.commands import io
    from alpha.cli.commands._types import DispatchResult

    errors = []
    monkeypatch.setattr(io, "print_error", lambda m: errors.append(m))
    # a transcribe call would mean the file was about to be uploaded — fail if reached
    import alpha.audio as _audio
    monkeypatch.setattr(_audio, "transcribe", lambda p: pytest.fail("must not upload outside-workspace file"))

    res = io.handle_audio(ctx=None, user_input="/audio /etc/hostname", parts=["/audio", "/etc/hostname"])
    assert res == DispatchResult.CONTINUE
    assert errors and "workspace" in errors[0].lower()
