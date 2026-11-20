"""Regression tests for DEEP_SECURITY V3.4 BAIXO fixes
(#012, #D129, #D131/#D144, #D139, #D141, #D142)."""
from __future__ import annotations

import os
import stat

import pytest


# ── #012: sanitize_for_log covers vendor key shapes ──

def test_sanitize_redacts_vendor_keys():
    from alpha._security_log import sanitize_for_log
    keys = [
        "AIzaSyAbcdefghijklmnopqrstuvwxyz0123456",   # Google
        "xai-abcdefghijklmnop1234",                  # xAI
        "hf_abcdefghijklmnop1234",                   # HuggingFace
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",  # GitHub
        "AKIAIOSFODNN7EXAMPLE",                       # AWS
        "sk_live_abcdefghijklmnop",                   # Stripe
    ]
    for k in keys:
        out = sanitize_for_log(f"token={k} tail")
        assert k not in out and "[redacted]" in out, k


# ── #D129: safe_env strips loader-injection vars ──

def test_safe_env_strips_loader_vars(monkeypatch):
    from alpha.tools import safe_env
    monkeypatch.setenv("LD_PRELOAD", "/evil.so")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/evil.dylib")
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/evil")
    monkeypatch.setenv("PERL5LIB", "/evil")
    monkeypatch.setenv("ALPHA_TEST_KEEPME", "ok")
    env = safe_env._build_safe_env()
    for k in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH",
              "NODE_OPTIONS", "PERL5LIB"):
        assert k not in env, k
    assert env.get("ALPHA_TEST_KEEPME") == "ok"


# ── #D139: workspace forbidden check is prefix-aware ──

@pytest.mark.parametrize("path,forbidden", [
    ("/proc/self", True), ("/etc/x", True), ("/sys/kernel", True),
    ("/", True), ("/etc", True), ("/run/x", True), ("/boot/x", True),
    ("/home", True), ("/tmp", True),            # exact system roots
    ("/home/u/proj", False), ("/opt/app", False),  # legit subdirs
    ("/mnt/data/proj", False), ("/tmp/x", False),
])
def test_forbidden_workspace_prefix(path, forbidden):
    from alpha.tools.workspace import _is_forbidden_workspace
    from pathlib import Path
    assert _is_forbidden_workspace(Path(path)) is forbidden


# ── #D141: apify actor_id format validation ──

@pytest.mark.asyncio
async def test_apify_rejects_malformed_actor_id():
    from alpha.tools import apify_tools
    for bad in ("../datasets/x", "a/b/c", "a b", "x?y=1", "../../store"):
        out = await apify_tools._run_actor(bad)
        assert "error" in out and "Invalid actor_id" in out["error"], bad


def test_apify_actor_id_regex_accepts_valid():
    from alpha.tools.apify_tools import _ACTOR_ID_RE
    for good in ("apify/web-scraper", "web-scraper", "user~actor", "abc123"):
        assert _ACTOR_ID_RE.match(good), good


# ── #D142: MCP env blocklist covers loader vars ──

def test_mcp_env_blocklist_has_loaders():
    from alpha.mcp.client import _MCP_ENV_BLOCKLIST
    for k in ("GCONV_PATH", "NODE_OPTIONS", "PERL5LIB", "RUBYLIB",
              "JAVA_TOOL_OPTIONS", "LD_PRELOAD"):
        assert k in _MCP_ENV_BLOCKLIST, k


# ── #D131 / #D144: memory write is 0o600 + symlink-refusing ──

def test_memory_write_perms_and_symlink_refusal(tmp_path, monkeypatch):
    import alpha.memory as mem
    target = tmp_path / "global.md"
    monkeypatch.setattr(mem, "_path_for", lambda scope: target)

    mem._write("global", "hello")
    assert target.read_text() == "hello"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"

    # #D144: refuse to write through a symlinked target.
    victim = tmp_path / "victim"
    victim.write_text("important")
    link = tmp_path / "link.md"
    link.symlink_to(victim)
    monkeypatch.setattr(mem, "_path_for", lambda scope: link)
    mem._write("global", "overwrite")  # logs + no-op, does not follow the link
    assert victim.read_text() == "important"
