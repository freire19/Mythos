"""Testes das correcoes DEEP_BUGS wave 3 — skills/wizard/sandbox.

- #DB26 — indice de instalacao corrompido nao crasha `update`/`list_installed`
- #DB28 — wizard nao levanta StopIteration com ALPHA_PROVIDER obsoleto
- #DB29 — sandbox.load_config tolera settings.json malformado
"""

from __future__ import annotations

import json

import pytest

from alpha import shell_sandbox as sandbox
from alpha.skills import manager
from alpha.wizard import steps


# ─── #DB26 — indice corrompido em skills/manager ─────────────────


@pytest.fixture
def user_skills_dir(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(manager, "USER_SKILLS_DIR", skills_dir)
    monkeypatch.setattr(manager, "INSTALLED_INDEX", skills_dir / ".installed.json")
    return skills_dir


def _make_skill(skills_dir, name):
    d = skills_dir / name
    d.mkdir()
    (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")


def _write_index(skills_dir, payload):
    (skills_dir / ".installed.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_list_installed_skips_non_dict_entry(user_skills_dir):
    _make_skill(user_skills_dir, "boa")
    _make_skill(user_skills_dir, "ruim")
    _write_index(user_skills_dir, {
        "boa": {"source": "/algum/lugar", "kind": "path", "installed_at": ""},
        "ruim": "nao-sou-dict",
    })

    rows = manager.list_installed()

    by_name = {r["name"]: r for r in rows}
    assert by_name["boa"]["tracked"] is True
    # entrada nao-dict cai no caminho local-untracked em vez de TypeError
    assert by_name["ruim"]["tracked"] is False
    assert by_name["ruim"]["source"] == "local-untracked"


def test_update_skips_entry_without_source(user_skills_dir, monkeypatch):
    _write_index(user_skills_dir, {
        "sem-source": {"kind": "path"},
        "escalar": 42,
        "boa": {"source": "/origem/boa", "kind": "path"},
    })

    calls = []

    def fake_install(source, name_override=None, force=False):
        calls.append(source)
        return manager.InstallResult(installed=["boa"])

    monkeypatch.setattr(manager, "install", fake_install)

    result = manager.update()

    assert calls == ["/origem/boa"]
    assert result.installed == ["boa"]
    assert len(result.errors) == 2
    assert any("sem-source" in e for e in result.errors)
    assert any("escalar" in e for e in result.errors)


# ─── #DB28 — ALPHA_PROVIDER obsoleto no wizard ───────────────────


def test_step_provider_obsolete_default_falls_back(monkeypatch):
    monkeypatch.setattr(steps, "ask_choice", lambda q, choices, default: default)

    provider = steps.step_provider({"ALPHA_PROVIDER": "provider-removido"})

    assert provider == steps._PROVIDERS[0]


def test_step_provider_valid_choice_still_matches(monkeypatch):
    valid = steps._PROVIDERS[-1]["id"]
    monkeypatch.setattr(steps, "ask_choice", lambda q, choices, default: valid)

    provider = steps.step_provider({})

    assert provider["id"] == valid


# ─── #DB29 — sandbox.load_config com settings malformado ─────────


def _write_settings(tmp_path, monkeypatch, payload):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPHA_SANDBOX", raising=False)
    cfg_dir = tmp_path / ".alpha"
    cfg_dir.mkdir()
    (cfg_dir / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def test_load_config_top_level_non_dict(tmp_path, monkeypatch):
    _write_settings(tmp_path, monkeypatch, ["nao", "sou", "dict"])
    cfg = sandbox.load_config()
    assert cfg.enabled is False
    assert cfg.tool == "auto"


def test_load_config_sandbox_scalar(tmp_path, monkeypatch):
    _write_settings(tmp_path, monkeypatch, {"sandbox": "true"})
    cfg = sandbox.load_config()
    assert cfg.enabled is False
    assert cfg.extra_args == []


def test_load_config_extra_args_non_list(tmp_path, monkeypatch):
    _write_settings(
        tmp_path,
        monkeypatch,
        {"sandbox": {"enabled": True, "extra_args": "--net=none"}},
    )
    cfg = sandbox.load_config()
    assert cfg.enabled is True
    assert cfg.extra_args == []


def test_load_config_well_formed_still_works(tmp_path, monkeypatch):
    _write_settings(
        tmp_path,
        monkeypatch,
        {"sandbox": {"enabled": True, "tool": "firejail", "extra_args": ["--x"]}},
    )
    cfg = sandbox.load_config()
    assert cfg.enabled is True
    assert cfg.tool == "firejail"
    assert cfg.extra_args == ["--x"]
