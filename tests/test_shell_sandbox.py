"""Tests for `alpha.shell_sandbox` (Plano-Upgrade-v3 H3 #14).

Sandbox backends (firejail/bubblewrap) aren't required by the test suite
— `resolve_tool` is monkeypatched so we test the wrapper logic without
needing the binaries installed in CI.
"""

from __future__ import annotations

import json

import pytest

from alpha import shell_sandbox as sandbox


# ─── load_config ─────────────────────────────────────────────────


def _write_settings(tmp_path, monkeypatch, payload):
    """Plant a settings.json the loader will find via find_config_file."""
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / ".alpha"
    cfg_dir.mkdir()
    (cfg_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_config_default_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    cfg = sandbox.load_config()
    assert cfg.enabled is False
    assert cfg.tool == "auto"


def test_load_config_reads_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(
        tmp_path,
        monkeypatch,
        {
            "sandbox": {
                "enabled": True,
                "tool": "firejail",
                "deny_network": False,
                "extra_args": ["--blacklist=/etc/shadow"],
            }
        },
    )
    cfg = sandbox.load_config()
    assert cfg.enabled is True
    assert cfg.tool == "firejail"
    assert cfg.deny_network is False
    assert cfg.extra_args == ["--blacklist=/etc/shadow"]


def test_load_config_env_var_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHA_SANDBOX", "1")
    cfg = sandbox.load_config()
    assert cfg.enabled is True


def test_load_config_rejects_invalid_tool(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(
        tmp_path, monkeypatch, {"sandbox": {"enabled": True, "tool": "selinux"}}
    )
    cfg = sandbox.load_config()
    # Unknown values fall back to auto rather than propagating.
    assert cfg.tool == "auto"


# ─── resolve_tool ────────────────────────────────────────────────


def test_resolve_tool_firejail_present(monkeypatch):
    def fake_which(name):
        return f"/usr/bin/{name}" if name == "firejail" else None

    monkeypatch.setattr(sandbox.shutil, "which", fake_which)
    assert sandbox.resolve_tool("firejail") == ("firejail", "/usr/bin/firejail")


def test_resolve_tool_firejail_missing(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    assert sandbox.resolve_tool("firejail") is None


def test_resolve_tool_auto_prefers_firejail(monkeypatch):
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda n: {"firejail": "/u/bin/firejail", "bwrap": "/u/bin/bwrap"}.get(n),
    )
    name, _ = sandbox.resolve_tool("auto")
    assert name == "firejail"


def test_resolve_tool_auto_falls_back_to_bwrap(monkeypatch):
    monkeypatch.setattr(
        sandbox.shutil, "which", lambda n: "/u/bin/bwrap" if n == "bwrap" else None
    )
    name, _ = sandbox.resolve_tool("auto")
    assert name == "bubblewrap"


def test_resolve_tool_auto_none_available(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    assert sandbox.resolve_tool("auto") is None


# ─── wrap_command ────────────────────────────────────────────────


def test_wrap_command_passthrough_when_disabled():
    cfg = sandbox.SandboxConfig(enabled=False)
    assert sandbox.wrap_command(["ls", "-la"], cfg=cfg) == ["ls", "-la"]


def test_wrap_command_raises_when_no_backend(monkeypatch):
    monkeypatch.setattr(sandbox, "resolve_tool", lambda _: None)
    cfg = sandbox.SandboxConfig(enabled=True, tool="firejail")
    with pytest.raises(sandbox.SandboxUnavailableError, match="no backend"):
        sandbox.wrap_command(["ls"], cfg=cfg)


def test_wrap_command_firejail_prefix(monkeypatch):
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("firejail", "/usr/bin/firejail")
    )
    cfg = sandbox.SandboxConfig(enabled=True, tool="firejail", deny_network=True)
    wrapped = sandbox.wrap_command(["git", "status"], cfg=cfg)
    assert wrapped[0] == "/usr/bin/firejail"
    assert "--net=none" in wrapped
    assert "--private-tmp" in wrapped
    # The actual command must be at the tail.
    assert wrapped[-2:] == ["git", "status"]


def test_wrap_command_firejail_allows_network_when_configured(monkeypatch):
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("firejail", "/usr/bin/firejail")
    )
    cfg = sandbox.SandboxConfig(enabled=True, deny_network=False)
    wrapped = sandbox.wrap_command(["curl", "https://example.com"], cfg=cfg)
    assert "--net=none" not in wrapped


def test_wrap_command_firejail_extra_args(monkeypatch):
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("firejail", "/usr/bin/firejail")
    )
    cfg = sandbox.SandboxConfig(
        enabled=True, extra_args=["--blacklist=/etc/shadow"]
    )
    wrapped = sandbox.wrap_command(["ls"], cfg=cfg)
    assert "--blacklist=/etc/shadow" in wrapped


def test_wrap_command_bwrap_layout(monkeypatch):
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("bubblewrap", "/usr/bin/bwrap")
    )
    cfg = sandbox.SandboxConfig(enabled=True, tool="bubblewrap", deny_network=True)
    wrapped = sandbox.wrap_command(["ls"], cwd="/work/proj", cfg=cfg)

    assert wrapped[0] == "/usr/bin/bwrap"
    # Read-only root bind so binaries resolve.
    assert "--ro-bind" in wrapped and "/" in wrapped
    # Tmpfs for /tmp so the sandboxed process can write scratch.
    assert "--tmpfs" in wrapped
    # cwd is rebound read-write and chdir'd to.
    assert "--bind" in wrapped
    assert "/work/proj" in wrapped
    assert "--chdir" in wrapped
    # Network is isolated.
    assert "--unshare-net" in wrapped
    # Actual command follows --.
    assert wrapped[-1] == "ls"


def test_wrap_command_bwrap_no_cwd(monkeypatch):
    """When cwd is None, bwrap shouldn't include --bind/--chdir lines."""
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("bubblewrap", "/usr/bin/bwrap")
    )
    cfg = sandbox.SandboxConfig(enabled=True, tool="bubblewrap")
    wrapped = sandbox.wrap_command(["ls"], cwd=None, cfg=cfg)
    # `--bind` is reserved for the cwd rebind; without cwd it shouldn't appear.
    assert "--bind" not in wrapped


# ─── describe / is_enabled ───────────────────────────────────────


def test_describe_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    assert "disabled" in sandbox.describe()


def test_describe_enabled_with_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(tmp_path, monkeypatch, {"sandbox": {"enabled": True}})
    monkeypatch.setattr(
        sandbox, "resolve_tool", lambda _: ("firejail", "/usr/bin/firejail")
    )
    out = sandbox.describe()
    assert "firejail" in out and "no network" in out


def test_describe_enabled_but_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(tmp_path, monkeypatch, {"sandbox": {"enabled": True}})
    monkeypatch.setattr(sandbox, "resolve_tool", lambda _: None)
    out = sandbox.describe()
    assert "NO BACKEND" in out


def test_is_enabled_reflects_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sandbox.is_enabled() is False

    monkeypatch.setenv("ALPHA_SANDBOX", "1")
    assert sandbox.is_enabled() is True


# ─── integration with run_subprocess_safe ────────────────────────


@pytest.mark.asyncio
async def test_run_subprocess_safe_passthrough_when_sandbox_disabled(tmp_path, monkeypatch):
    """When sandbox is disabled, sandbox=True is a no-op — the command
    still runs natively. This confirms the lazy import in the helper
    doesn't accidentally break the passthrough path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)

    from alpha.tools._subprocess_helpers import run_subprocess_safe

    result = await run_subprocess_safe(
        "echo", "hello", timeout=5, sandbox=True
    )
    assert result.returncode == 0
    assert b"hello" in result.stdout


@pytest.mark.asyncio
async def test_run_subprocess_safe_raises_when_sandbox_unavailable(
    tmp_path, monkeypatch
):
    """Enabled sandbox with no backend → SandboxUnavailableError surfaces
    out of run_subprocess_safe, not a silent unsandboxed execution."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHA_SANDBOX", "1")
    monkeypatch.setattr(sandbox, "resolve_tool", lambda _: None)

    from alpha.tools._subprocess_helpers import run_subprocess_safe

    with pytest.raises(sandbox.SandboxUnavailableError):
        await run_subprocess_safe("echo", "hi", timeout=5, sandbox=True)
