"""DEEP_MAINTAINABILITY wave 2 — cluster config/infra.

Cobre o comportamento preservado (e as unificações) de:
- #DM061: `config.env_bool` — vocabulário canônico de env-boolean,
  roteado por jsonlogs / checkpoints / vision_tool_results_enabled.
- #DM066: `settings.atomic_write_text` — escrita atômica/durável única,
  roteada por history / checkpoints / wizard.env / skills.manager.
- #DM075: caps do resumo de sessão da history viram constantes nomeadas.
"""

from __future__ import annotations

import json
import stat

import pytest

from alpha import checkpoints, history
from alpha.config import _tristate_flag, env_bool, vision_tool_results_enabled
from alpha.settings import atomic_write_text


# ─── #DM061: env_bool ────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", " on ", "Yes"])
def test_env_bool_truthy_vocabulary(monkeypatch, raw):
    monkeypatch.setenv("ALPHA_TEST_FLAG", raw)
    assert env_bool("ALPHA_TEST_FLAG", default=False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF", " 0 "])
def test_env_bool_falsy_vocabulary(monkeypatch, raw):
    monkeypatch.setenv("ALPHA_TEST_FLAG", raw)
    assert env_bool("ALPHA_TEST_FLAG", default=True) is False


@pytest.mark.parametrize("default", [True, False])
def test_env_bool_unset_and_garbage_fall_back_to_default(monkeypatch, default):
    monkeypatch.delenv("ALPHA_TEST_FLAG", raising=False)
    assert env_bool("ALPHA_TEST_FLAG", default=default) is default
    monkeypatch.setenv("ALPHA_TEST_FLAG", "banana")
    assert env_bool("ALPHA_TEST_FLAG", default=default) is default
    monkeypatch.setenv("ALPHA_TEST_FLAG", "auto")
    assert env_bool("ALPHA_TEST_FLAG", default=default) is default


def test_tristate_flag_delegates_to_env_bool(monkeypatch):
    monkeypatch.setenv("ALPHA_TEST_FLAG", "on")
    assert _tristate_flag("ALPHA_TEST_FLAG", auto_default=False) is True
    monkeypatch.setenv("ALPHA_TEST_FLAG", "off")
    assert _tristate_flag("ALPHA_TEST_FLAG", auto_default=True) is False
    monkeypatch.setenv("ALPHA_TEST_FLAG", "auto")
    assert _tristate_flag("ALPHA_TEST_FLAG", auto_default=True) is True


def test_checkpoints_enabled_preserved(monkeypatch):
    # Kill-switch: default ON; só o vocabulário falsy desliga.
    monkeypatch.delenv("ALPHA_CHECKPOINTS", raising=False)
    assert checkpoints.enabled() is True
    monkeypatch.setenv("ALPHA_CHECKPOINTS", "0")
    assert checkpoints.enabled() is False
    monkeypatch.setenv("ALPHA_CHECKPOINTS", "off")
    assert checkpoints.enabled() is False
    # Valor não reconhecido continua caindo no default True (como antes).
    monkeypatch.setenv("ALPHA_CHECKPOINTS", "banana")
    assert checkpoints.enabled() is True


def test_jsonlogs_gate_uses_canonical_vocabulary(monkeypatch):
    """`ALPHA_JSON_LOGS=on` (e com espaços) agora conta como habilitado.

    Testa só o gate (sem instalar handler): com o flag DESLIGADO por
    vocabulário falsy, `maybe_install` retorna None sem tocar logging.
    """
    from alpha import jsonlogs

    monkeypatch.setattr(jsonlogs, "_installed", False)
    monkeypatch.setenv("ALPHA_JSON_LOGS", "off")
    assert jsonlogs.maybe_install() is None
    monkeypatch.setenv("ALPHA_JSON_LOGS", "banana")
    assert jsonlogs.maybe_install() is None


def test_vision_override_env_wins_even_with_unknown_provider(monkeypatch):
    monkeypatch.setenv("ALPHA_VISION_TOOL_RESULTS", "on")
    assert vision_tool_results_enabled("provider-que-nao-existe") is True
    monkeypatch.setenv("ALPHA_VISION_TOOL_RESULTS", "off")
    assert vision_tool_results_enabled("provider-que-nao-existe") is False
    # Sem override: provider desconhecido → fail-closed.
    monkeypatch.delenv("ALPHA_VISION_TOOL_RESULTS", raising=False)
    assert vision_tool_results_enabled("provider-que-nao-existe") is False


# ─── #DM066: atomic_write_text ───────────────────────────────────


def test_atomic_write_text_roundtrip_mode_and_no_tmp_leftover(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_text(target, '{"k": "ação"}')
    assert target.read_text(encoding="utf-8") == '{"k": "ação"}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_text_overwrites_existing(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_text_custom_mode(tmp_path):
    target = tmp_path / "wide.txt"
    atomic_write_text(target, "x", mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_text_replaces_symlink_instead_of_following(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("intocado")
    link = tmp_path / "link.txt"
    link.symlink_to(victim)
    atomic_write_text(link, "novo")
    # O symlink é SUBSTITUÍDO por um arquivo regular; o alvo não é tocado.
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "novo"
    assert victim.read_text(encoding="utf-8") == "intocado"


def test_history_atomic_write_still_refuses_symlink(tmp_path):
    # Contrato específico da history (#D109) preservado após o roteamento.
    victim = tmp_path / "victim.json"
    victim.write_text("{}")
    link = tmp_path / "sess.json"
    link.symlink_to(victim)
    with pytest.raises(OSError, match="refusing to write through symlink"):
        history._atomic_write(link, "{}")


def test_checkpoints_write_index_roundtrip(tmp_path):
    entries = [
        {"seq": 1, "kind": "marker", "label": "turn"},
        {"seq": 2, "kind": "edit", "path": "a.py"},
    ]
    checkpoints._write_index(tmp_path, entries)
    assert checkpoints._read_index(tmp_path) == entries
    idx = tmp_path / "index.jsonl"
    assert stat.S_IMODE(idx.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_wizard_write_env_merge_and_mode(tmp_path, monkeypatch):
    from alpha.wizard import env as wenv

    env_path = tmp_path / ".env"
    env_path.write_text("# comentario\nA=1\nB=2\n", encoding="utf-8")
    monkeypatch.setattr(wenv, "ENV_PATH", env_path)

    out = wenv.write_env({"B": "novo", "C": "3"})
    assert out == env_path
    text = env_path.read_text(encoding="utf-8")
    assert text == "# comentario\nA=1\nB=novo\nC=3\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".env.*")) == []  # sem tmp órfão


def test_wizard_write_env_still_rejects_newline(tmp_path, monkeypatch):
    from alpha.wizard import env as wenv

    monkeypatch.setattr(wenv, "ENV_PATH", tmp_path / ".env")
    with pytest.raises(ValueError, match="Newline"):
        wenv.write_env({"K": "a\nB=evil"})


def test_skills_save_index_roundtrip(tmp_path, monkeypatch):
    from alpha.skills import manager

    monkeypatch.setattr(manager, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(manager, "INSTALLED_INDEX", tmp_path / ".installed.json")

    data = {"minha-skill": {"source": "git", "url": "https://x/y.git"}}
    manager._save_index(data)
    on_disk = json.loads((tmp_path / ".installed.json").read_text(encoding="utf-8"))
    assert on_disk == data
    assert manager._load_index() == data
    assert list(tmp_path.glob("*.tmp")) == []


# ─── #DM075: caps nomeados do resumo de sessão ───────────────────


def test_session_summary_caps_preserved():
    long = "x" * 1000
    msgs = [
        {"role": "user", "content": long},
        {"role": "assistant", "content": long},
        {"role": "tool", "content": long},
    ]
    parts = history._build_session_summary(msgs).split("\n")
    assert parts[0] == f"[user]: {'x' * 300}"
    assert parts[1] == f"[assistant]: {'x' * 500}"
    assert parts[2] == f"[tool result]: {'x' * 200}"


def test_session_summary_keeps_last_40_parts():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(60)]
    parts = history._build_session_summary(msgs).split("\n")
    assert len(parts) == 40
    assert parts[0] == "[user]: m20"
    assert parts[-1] == "[user]: m59"
