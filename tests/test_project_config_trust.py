"""Fail-closed trust gate for project-scoped `.alpha` config that executes
commands (#D301 hooks · #D305 MCP stdio servers).

A `.alpha/settings.json` (hooks) or `.alpha/mcp.json` (stdio server) planted in
an untrusted repo must NOT run shell / spawn a process just because `alpha` was
launched in that directory. Only `~/.alpha` is trusted by default; project
config requires the explicit opt-in `ALPHA_TRUST_PROJECT_CONFIG`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha import hooks, settings
from alpha.mcp.config import load_mcp_config


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Start every test with the opt-in UNSET and a fresh hook cache.
    monkeypatch.delenv("ALPHA_TRUST_PROJECT_CONFIG", raising=False)
    hooks.reset_cache()
    yield
    hooks.reset_cache()


# ── settings helpers ──────────────────────────────────────────────


class TestTrustHelpers:
    def test_project_config_trusted_truthy(self, monkeypatch):
        for val in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", val)
            assert settings.project_config_trusted() is True

    def test_project_config_trusted_falsy(self, monkeypatch):
        for val in ("", "0", "false", "no", "off", "nope"):
            monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", val)
            assert settings.project_config_trusted() is False

    def test_is_user_scoped_config(self, tmp_path, monkeypatch):
        user_dir = tmp_path / "home_alpha"
        user_dir.mkdir()
        monkeypatch.setattr(
            settings, "alpha_user_dir",
            lambda subdir="": (user_dir / subdir) if subdir else user_dir,
        )
        assert settings.is_user_scoped_config(user_dir / "settings.json") is True
        assert settings.is_user_scoped_config(
            tmp_path / "proj" / ".alpha" / "settings.json"
        ) is False
        assert settings.is_user_scoped_config(None) is False


# ── hooks (#D301) ─────────────────────────────────────────────────


def _hooks_parsed():
    return hooks._parse_hooks({"session_start": [{"command": "true"}]})


class TestHookGate:
    def test_gate_drops_project_hooks_without_optin(self, tmp_path):
        path = tmp_path / ".alpha" / "settings.json"  # project-scoped
        gated = hooks._gate_project_hooks(_hooks_parsed(), path)
        assert gated["session_start"] == []

    def test_gate_keeps_project_hooks_with_optin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        path = tmp_path / ".alpha" / "settings.json"
        gated = hooks._gate_project_hooks(_hooks_parsed(), path)
        assert len(gated["session_start"]) == 1

    def test_gate_keeps_user_scoped_hooks_without_optin(self, tmp_path, monkeypatch):
        user_dir = tmp_path / "home_alpha"
        user_dir.mkdir()
        monkeypatch.setattr(
            settings, "alpha_user_dir",
            lambda subdir="": (user_dir / subdir) if subdir else user_dir,
        )
        gated = hooks._gate_project_hooks(_hooks_parsed(), user_dir / "settings.json")
        assert len(gated["session_start"]) == 1

    def test_load_hooks_drops_cwd_config_by_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        alpha_dir = tmp_path / ".alpha"
        alpha_dir.mkdir()
        (alpha_dir / "settings.json").write_text(
            json.dumps({"hooks": {"session_start": [{"command": "true"}]}}),
            encoding="utf-8",
        )
        loaded = hooks.load_hooks(force=True)
        assert loaded["session_start"] == []
        assert hooks.has_event("session_start") is False

    def test_load_hooks_loads_cwd_config_when_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        monkeypatch.chdir(tmp_path)
        alpha_dir = tmp_path / ".alpha"
        alpha_dir.mkdir()
        (alpha_dir / "settings.json").write_text(
            json.dumps({"hooks": {"session_start": [{"command": "true"}]}}),
            encoding="utf-8",
        )
        loaded = hooks.load_hooks(force=True)
        assert len(loaded["session_start"]) == 1


# ── MCP stdio (#D305) ─────────────────────────────────────────────


def _write_mcp(path: Path, servers: dict) -> Path:
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


class TestMcpServerGate:
    def test_project_stdio_dropped_by_default(self, tmp_path):
        p = _write_mcp(
            tmp_path / "mcp.json",
            {"fs": {"command": "echo", "args": ["hi"]}},
        )
        assert load_mcp_config(p) == []

    def test_project_stdio_loaded_when_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        p = _write_mcp(
            tmp_path / "mcp.json",
            {"fs": {"command": "echo", "args": ["hi"]}},
        )
        servers = load_mcp_config(p)
        assert len(servers) == 1
        assert servers[0].command == "echo"

    def test_project_http_dropped_by_default(self, tmp_path):
        # #D305: http servers from project config are also fail-closed — a
        # planted url with `${VAR}` headers would exfil secrets on connect.
        p = _write_mcp(
            tmp_path / "mcp.json",
            {"remote": {"url": "https://mcp.example.com/mcp"}},
        )
        assert load_mcp_config(p) == []

    def test_project_http_loaded_when_trusted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
        p = _write_mcp(
            tmp_path / "mcp.json",
            {"remote": {"url": "https://mcp.example.com/mcp"}},
        )
        servers = load_mcp_config(p)
        assert len(servers) == 1
        assert servers[0].is_http is True
