"""Testes das flags --continue/--resume da CLI (main.py).

Cobre o resolver de sessão e a presença das flags no parser.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import alpha.cli.entry as alpha_main

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestResolveResumeMessages:
    def test_continue_uses_last_session(self, monkeypatch):
        monkeypatch.setattr(alpha_main, "get_last_session_id", lambda: "sess-123")
        monkeypatch.setattr(
            alpha_main,
            "load_session",
            lambda sid: [{"role": "user", "content": "oi"}] if sid == "sess-123" else None,
        )
        resolved = alpha_main._resolve_resume_messages("__last__")
        assert resolved == ("sess-123", [{"role": "user", "content": "oi"}])

    def test_resume_explicit_id(self, monkeypatch):
        monkeypatch.setattr(
            alpha_main,
            "load_session",
            lambda sid: [{"role": "assistant", "content": "ok"}] if sid == "abc" else None,
        )
        resolved = alpha_main._resolve_resume_messages("abc")
        assert resolved == ("abc", [{"role": "assistant", "content": "ok"}])

    def test_no_last_session_returns_none(self, monkeypatch):
        monkeypatch.setattr(alpha_main, "get_last_session_id", lambda: None)
        assert alpha_main._resolve_resume_messages("__last__") is None

    def test_missing_session_returns_none(self, monkeypatch):
        monkeypatch.setattr(alpha_main, "load_session", lambda sid: None)
        assert alpha_main._resolve_resume_messages("nao-existe") is None


def test_cli_help_exposes_resume_flags():
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    assert "--continue" in proc.stdout
    assert "--resume" in proc.stdout
