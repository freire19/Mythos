"""DEEP_RESILIENCE #D807 — settings.json malformado deve falhar-FECHADO.

Um `settings.json` presente mas improcessável (JSONDecodeError) não pode ser
tratado como "ausente": o sandbox opt-in viraria passthrough silencioso e
todos os hooks (inclusive `pre_tool` blocking) seriam dropados e cacheados
como vazios pelo resto da sessão.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha import hooks
from alpha import shell_sandbox as sandbox  # Mythos: alpha.sandbox e o sandbox de exploit
from alpha._json_utils import JSON_INVALID, load_json_file
from alpha.settings import read_json_or_invalid


# ─── _json_utils: sentinela distingue "ausente" de "corrompido" ──


def test_missing_file_returns_default_not_sentinel(tmp_path):
    result = load_json_file(
        tmp_path / "missing.json", default={}, invalid_default=JSON_INVALID
    )
    assert result == {}
    assert result is not JSON_INVALID


def test_corrupt_file_returns_sentinel(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{bad json,", encoding="utf-8")
    assert load_json_file(p, default={}, invalid_default=JSON_INVALID) is JSON_INVALID


def test_corrupt_file_without_optin_keeps_old_behavior(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{bad json,", encoding="utf-8")
    assert load_json_file(p, default={"d": 1}) == {"d": 1}


def test_read_json_or_invalid_valid_file_parses(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    assert read_json_or_invalid(p, default={}) == {"a": 1}


def test_read_json_or_invalid_none_path_returns_default():
    assert read_json_or_invalid(None, default={}) == {}


# ─── sandbox: corrupto → enabled-but-broken → SandboxUnavailableError ──


def _write_settings(tmp_path: Path, monkeypatch, text: str) -> Path:
    monkeypatch.chdir(tmp_path)
    cfg_dir = tmp_path / ".alpha"
    cfg_dir.mkdir()
    path = cfg_dir / "settings.json"
    path.write_text(text, encoding="utf-8")
    return path


def test_sandbox_corrupt_settings_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(tmp_path, monkeypatch, '{"sandbox": {"enabled": true},}')
    cfg = sandbox.load_config()
    assert cfg.enabled is True
    assert cfg.config_error
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.wrap_command(["echo", "hi"])


def test_sandbox_corrupt_settings_even_if_sandbox_block_absent(tmp_path, monkeypatch):
    # Um typo em QUALQUER parte do arquivo torna o estado do opt-in incognoscível.
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(tmp_path, monkeypatch, '{"hooks": {,}}')
    with pytest.raises(sandbox.SandboxUnavailableError):
        sandbox.wrap_command(["true"])


def test_sandbox_missing_settings_still_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sandbox, "find_config_file", lambda _name: None)
    assert sandbox.wrap_command(["echo", "hi"]) == ["echo", "hi"]


def test_sandbox_valid_disabled_still_passthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(
        tmp_path, monkeypatch, json.dumps({"sandbox": {"enabled": False}})
    )
    cfg = sandbox.load_config()
    assert cfg.enabled is False
    assert cfg.config_error == ""
    assert sandbox.wrap_command(["echo", "hi"]) == ["echo", "hi"]


def test_sandbox_describe_reports_broken_config(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    _write_settings(tmp_path, monkeypatch, "{oops")
    assert "BROKEN CONFIG" in sandbox.describe()


# ─── hooks: corrupto → WARNING + sem cache (recarrega na próxima) ──


@pytest.fixture(autouse=True)
def _reset_hook_cache():
    hooks.reset_cache()
    yield
    hooks.reset_cache()


def test_hooks_corrupt_settings_warns_and_returns_empty(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
    _write_settings(tmp_path, monkeypatch, "{bad json,")
    import logging

    with caplog.at_level(logging.WARNING, logger="alpha.hooks"):
        loaded = hooks.load_hooks(force=True)
    assert all(loaded[ev] == [] for ev in hooks.VALID_EVENTS)
    assert any("could not be parsed" in rec.message for rec in caplog.records)


def test_hooks_corrupt_settings_not_cached_reloads_after_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
    path = _write_settings(tmp_path, monkeypatch, "{bad json,")
    loaded = hooks.load_hooks()
    assert all(loaded[ev] == [] for ev in hooks.VALID_EVENTS)
    # Arquivo corrigido deve ser lido no próximo acesso SEM force.
    path.write_text(
        json.dumps({"hooks": {"pre_tool": [{"command": "true"}]}}),
        encoding="utf-8",
    )
    loaded = hooks.load_hooks()
    assert len(loaded["pre_tool"]) == 1


def test_hooks_valid_settings_still_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
    path = _write_settings(
        tmp_path,
        monkeypatch,
        json.dumps({"hooks": {"pre_tool": [{"command": "true"}]}}),
    )
    loaded = hooks.load_hooks(force=True)
    assert len(loaded["pre_tool"]) == 1
    # Cache continua valendo para arquivo válido.
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    assert len(hooks.load_hooks()["pre_tool"]) == 1


def test_hooks_fire_with_corrupt_settings_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_TRUST_PROJECT_CONFIG", "1")
    _write_settings(tmp_path, monkeypatch, "{bad json,")
    outcome = hooks.fire("pre_tool", tool_name="read_file", tool_args={})
    assert outcome.blocked is False
