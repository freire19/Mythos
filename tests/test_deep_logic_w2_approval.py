"""DEEP_LOGIC wave 2 — approval.py (#DL205, #DL206).

- #DL205: search_and_replace (DESTRUCTIVE, auto-aprovada) agora passa pelo
  gate de path sensivel — fecha o vetor plant+execute em .git/hooks e
  .alpha/*.json sem prompt.
- #DL206: web_fetch/web_search entram em _PRIMARY_ARG (rules arg-scoped
  passam a casar) e _parse_rule avisa quando um rule arg-scoped aponta para
  tool sem primary-arg mapeado (deny inerte = fail-open silencioso).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from alpha import approval


@pytest.fixture(autouse=True)
def _reset_cache():
    approval.reset_permission_cache()
    yield
    approval.reset_permission_cache()


@pytest.fixture
def settings_in_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    alpha_dir = tmp_path / ".alpha"
    alpha_dir.mkdir()
    return alpha_dir / "settings.json"


def _write_perms(path: Path, allow=None, deny=None) -> None:
    block = {}
    if allow is not None:
        block["allow"] = allow
    if deny is not None:
        block["deny"] = deny
    path.write_text(json.dumps({"permissions": block}), encoding="utf-8")


# ── #DL205: search_and_replace no gate de path sensivel ──


class TestSnrSensitivePathGate:
    def test_snr_git_hooks_requires_approval(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert approval.needs_approval(
            "search_and_replace",
            {"path": ".git/hooks/pre-commit", "search": "a", "replace": "b"},
        ) is True

    def test_snr_alpha_settings_requires_approval(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert approval.needs_approval(
            "search_and_replace",
            {"path": ".alpha/settings.json", "search": "a", "replace": "b"},
        ) is True

    def test_snr_normal_path_still_auto_approves(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert approval.needs_approval(
            "search_and_replace",
            {"path": "src/app.py", "search": "a", "replace": "b"},
        ) is False

    def test_broad_allow_rule_does_not_bypass_gate(self, settings_in_cwd: Path):
        # #D309 estendido: allow tool-name-only nao libera path sensivel.
        _write_perms(settings_in_cwd, allow=["search_and_replace"])
        assert approval.needs_approval(
            "search_and_replace",
            {"path": ".git/hooks/post-commit", "search": "a", "replace": "b"},
        ) is True

    def test_exact_path_allow_bypasses_gate(self, settings_in_cwd: Path):
        _write_perms(settings_in_cwd, allow=["search_and_replace(.git/hooks/post-commit)"])
        assert approval.needs_approval(
            "search_and_replace",
            {"path": ".git/hooks/post-commit", "search": "a", "replace": "b"},
        ) is False


# ── #DL206: web_fetch/web_search em _PRIMARY_ARG ──


class TestWebToolsPrimaryArg:
    def test_web_fetch_deny_by_url_now_matches(self, settings_in_cwd: Path):
        _write_perms(settings_in_cwd, deny=[r"web_fetch:internal\.corp"])
        denied, reason = approval.is_denied(
            "web_fetch", {"url": "https://internal.corp/wiki"}
        )
        assert denied is True
        assert "internal" in reason
        denied, _ = approval.is_denied("web_fetch", {"url": "https://example.com"})
        assert denied is False

    def test_web_search_deny_by_query_now_matches(self, settings_in_cwd: Path):
        _write_perms(settings_in_cwd, deny=["web_search(senha do wifi)"])
        denied, _ = approval.is_denied("web_search", {"query": "senha do wifi"})
        assert denied is True
        denied, _ = approval.is_denied("web_search", {"query": "docs pytest"})
        assert denied is False


# ── #DL206: warning para rule arg-scoped sem primary-arg ──


class TestArgScopedRuleWarning:
    def test_arg_scoped_rule_on_unmapped_tool_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="alpha.approval"):
            rule = approval._parse_rule("todo_write(x)")
        assert rule is not None
        assert any("never match" in r.message for r in caplog.records)

    def test_arg_scoped_regex_on_mcp_tool_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="alpha.approval"):
            rule = approval._parse_rule("mcp__server__tool:pattern")
        assert rule is not None
        assert any("never match" in r.message for r in caplog.records)

    def test_name_only_rule_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="alpha.approval"):
            rule = approval._parse_rule("todo_write")
        assert rule is not None
        assert not caplog.records

    def test_arg_scoped_rule_on_mapped_tool_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="alpha.approval"):
            rule = approval._parse_rule("execute_shell:^git ")
        assert rule is not None
        assert not caplog.records
