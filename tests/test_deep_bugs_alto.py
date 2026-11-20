"""Regression tests for the DEEP_BUGS V5.0 ALTO fixes.

- #DB01: search_and_replace must not inherit search_files' 50-line default
  (silent partial mass-replace reported as success).
- #DB02: needs_approval must not crash on non-string primary args from the model.
- #DB03: /history must survive assistant messages with content=None (tool turns).
"""

from __future__ import annotations

import types

import pytest

from alpha.approval import needs_approval


@pytest.fixture(autouse=True)
def _hermetic_approval(monkeypatch):
    # Approval decisions from the built-in defaults only (ignore any local
    # .alpha/settings.json), matching test_approval.py.
    from alpha import approval

    monkeypatch.setattr(approval, "find_config_file", lambda name: None)
    approval.reset_permission_cache()
    yield
    approval.reset_permission_cache()


class TestDB02NonStringPrimaryArgs:
    @pytest.mark.parametrize("tool,args", [
        ("write_file", {"content": ["a", "b"], "path": "/tmp/x.py"}),
        ("write_file", {"content": {"k": "v"}, "path": "/tmp/x.py"}),
        ("edit_file", {"content": 123, "path": "/tmp/x.py"}),
        ("execute_shell", {"command": ["ls", "-la"]}),
        ("execute_pipeline", {"pipeline": ["a", "b"]}),
        ("git_operation", {"action": ["log"]}),
        ("git_operation", {"action": {"x": 1}}),
    ])
    def test_non_string_primary_arg_does_not_crash(self, tool, args):
        # #DB02: previously AttributeError/TypeError escaped and killed the turn.
        # Now the arg is coerced to str → a valid bool decision, never a raise.
        assert isinstance(needs_approval(tool, args), bool)


class TestDB03HistoryNoneContent:
    def test_handle_history_survives_none_content(self):
        # #DB03: a tool-call assistant message has content=None; `None[:100]`
        # crashed the whole REPL after a /load. Must not raise.
        from alpha.cli.commands._types import DispatchResult
        from alpha.cli.commands.session import _handle_history

        ctx = types.SimpleNamespace(history=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None},   # tool-only turn
            {"role": "assistant"},                    # no content key at all
        ])
        assert _handle_history(ctx, ["/history"]) == DispatchResult.CONTINUE


class TestDB01SearchAndReplaceCap:
    @pytest.mark.asyncio
    async def test_uses_high_result_cap(self, tmp_path, monkeypatch):
        # #DB01: the file-discovery sub-call must pass an explicit high cap, not
        # inherit search_files' 50-line default (which silently truncated a mass
        # replace and reported it as a completed success).
        captured: dict = {}

        async def fake_run_tool(name, **kwargs):
            if name == "search_files":
                captured["max_results"] = kwargs.get("max_results")
            return {"results": []}

        monkeypatch.setattr(
            "alpha.tools._composite_snr._run_tool", fake_run_tool
        )
        monkeypatch.setattr(
            "alpha.tools._composite_snr._resolve_target",
            lambda p, **kw: (tmp_path, None),
        )
        from alpha.tools._composite_snr import _search_and_replace

        await _search_and_replace(str(tmp_path), "foo", "bar", dry_run=True)
        assert captured.get("max_results") and captured["max_results"] >= 1000
