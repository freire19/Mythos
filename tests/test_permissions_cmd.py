"""`/permissions` — regras allow/deny do settings.json pelo REPL.

Contratos:
- lista regras numeradas com a origem; marca regras inválidas;
- allow/deny validam a gramática antes de gravar, deduplicam, gravam no
  settings.json que o approval carrega (ou ~/.alpha/settings.json) e
  invalidam o cache — a próxima `needs_approval` já vê a regra;
- remove por texto ou por #n; erros legíveis; está no catálogo.
"""

from __future__ import annotations

import json

import pytest

from alpha import approval
from alpha.cli.commands import DispatchResult, _DESCRIPTIONS, _DISPATCH, permissions_cmd


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = tmp_path / ".alpha" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["read_file"], "deny": ["execute_shell:sudo"]}, "other": 1}))
    monkeypatch.setattr(approval, "_find_settings_file", lambda: settings)
    monkeypatch.setattr(approval, "_settings_write_path", lambda: settings)
    approval.reset_permission_cache()
    yield settings
    approval.reset_permission_cache()


def _run(line: str) -> DispatchResult:
    return permissions_cmd._handle_permissions(None, line.split())


class TestList:
    def test_lists_numbered(self, ws, capsys):
        assert _run("/permissions") is DispatchResult.CONTINUE
        out = capsys.readouterr().out
        assert str(ws) in out
        assert "#1" in out and "allow" in out and "read_file" in out
        assert "#2" in out and "deny" in out and "execute_shell:sudo" in out

    def test_flags_invalid_rule(self, ws, capsys):
        ws.write_text(json.dumps({"permissions": {"allow": ["bad rule!"]}}))
        _run("/permissions")
        assert "inválida" in capsys.readouterr().out

    def test_no_settings(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(approval, "_find_settings_file", lambda: None)
        _run("/permissions")
        assert "Sem regras do usuário" in capsys.readouterr().out


class TestEdit:
    def test_allow_and_deny_persist_and_apply(self, ws, capsys):
        assert approval.needs_approval("execute_shell", {"command": "npm test"}) is True
        _run("/permissions allow execute_shell:^npm ")
        data = json.loads(ws.read_text())
        assert data["permissions"]["allow"] == ["read_file", "execute_shell:^npm"]
        assert data["other"] == 1  # untouched keys survive
        assert approval.needs_approval("execute_shell", {"command": "npm test"}) is False
        _run("/permissions deny execute_shell(rm -rf /)")
        assert "execute_shell(rm -rf /)" in json.loads(ws.read_text())["permissions"]["deny"]
        assert approval.is_denied("execute_shell", {"command": "rm -rf /"})[0] is True
        out = capsys.readouterr().out
        assert "allow execute_shell:^npm" in out and "deny execute_shell(rm -rf /)" in out

    def test_duplicate_and_invalid(self, ws, capsys):
        _run("/permissions allow read_file")
        assert "já contém" in capsys.readouterr().out
        _run("/permissions allow 9bad")
        assert "Regra inválida" in capsys.readouterr().out
        assert json.loads(ws.read_text())["permissions"]["allow"] == ["read_file"]

    def test_remove_by_text_and_number(self, ws, capsys):
        _run("/permissions remove #2")
        assert json.loads(ws.read_text())["permissions"]["deny"] == []
        _run("/permissions remove read_file")
        assert json.loads(ws.read_text())["permissions"]["allow"] == []
        _run("/permissions remove nope")
        assert "não encontrada" in capsys.readouterr().out
        _run("/permissions remove #9")
        assert "Não existe regra" in capsys.readouterr().out

    def test_creates_file_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "home" / ".alpha" / "settings.json"
        monkeypatch.setattr(approval, "_find_settings_file", lambda: None)
        monkeypatch.setattr(approval, "_settings_write_path", lambda: target)
        _run("/permissions deny execute_shell:curl")
        assert json.loads(target.read_text())["permissions"]["deny"] == ["execute_shell:curl"]

    def test_usage(self, ws, capsys):
        for line in ("/permissions allow", "/permissions bogus x"):
            _run(line)
            assert "Usage" in capsys.readouterr().out

    def test_in_catalogue(self):
        assert "/permissions" in _DESCRIPTIONS and _DISPATCH["/permissions"] is permissions_cmd._handle_permissions
