"""Regression tests for the DEEP_SECURITY V3.4 MÉDIO/BAIXO hardening fixes.

Covers the security-critical behavior changes that are cleanly assertable:
- #D308: interpreter eval-flag guard uses prefix (glued `-c<code>`, `--eval=`).
- #D309: a sensitive-path write is not auto-approved by a broad allow-rule.
- #D310: AST blocklist catches aliased builtins (eval/exec/getattr).
- #D315: SSRF blocks CGNAT (Tailscale) and 6to4 ranges.
- #D316: unsafe control/ANSI/bidi stripped from displayed tool output.
"""

from __future__ import annotations

import pytest

from alpha.approval import is_safe_shell_command, needs_approval
from alpha.net_utils import is_private_ip_address
from alpha.tools.code_tools import _validate_code_safety


@pytest.fixture(autouse=True)
def _hermetic_approval(monkeypatch):
    from alpha import approval

    monkeypatch.setattr(approval, "find_config_file", lambda name: None)
    approval.reset_permission_cache()
    yield
    approval.reset_permission_cache()


class TestD308EvalFlagPrefix:
    @pytest.mark.parametrize("cmd", [
        "python3 -cprint(1)",          # glued -c
        'python3 -c"import os"',
        "node --eval=1",               # --eval=
        "node -e1",                    # glued -e
        "python3 -c 1",                # classic (space) still caught
    ])
    def test_glued_eval_requires_approval(self, cmd):
        assert is_safe_shell_command(cmd) is False


class TestD309SensitivePathGate:
    def test_sensitive_write_needs_approval_without_exact_allow(self):
        # No allow-rule at all → the sensitive-path gate fires.
        assert needs_approval("write_file", {"path": "~/.bashrc", "content": "x"}) is True
        assert needs_approval("edit_file", {"path": "~/.ssh/authorized_keys"}) is True

    def test_non_sensitive_write_auto_approves(self, tmp_path):
        p = str(tmp_path / "ok.py")
        assert needs_approval("write_file", {"path": p, "content": "x"}) is False


class TestD310AstAliasing:
    @pytest.mark.parametrize("code", [
        "e=eval\ne('1')",
        "[exec][0]('x')",
        "g=getattr\ng(1,'x')",
        "list(map(eval,['1']))",
    ])
    def test_aliased_builtin_blocked(self, code):
        assert _validate_code_safety(code) is not None

    def test_benign_code_allowed(self):
        assert _validate_code_safety("x = 1 + 1\ny = [i for i in range(3)]") is None


class TestD315SsrfRanges:
    @pytest.mark.parametrize("ip", ["100.64.0.1", "192.88.99.1", "10.0.0.5"])
    def test_internal_ranges_blocked(self, ip):
        assert is_private_ip_address(ip) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1"])
    def test_public_allowed(self, ip):
        assert is_private_ip_address(ip) is False


class TestD316DisplayStrip:
    def test_strips_ansi_and_bidi(self):
        from alpha.display.renderers.tools import _strip_control

        dirty = "hello\x1b[2Jworld\x07‮EVIL​"
        clean = _strip_control(dirty)
        assert "\x1b" not in clean
        assert "\x07" not in clean
        assert "‮" not in clean
        assert "​" not in clean
        assert "hello" in clean and "world" in clean
