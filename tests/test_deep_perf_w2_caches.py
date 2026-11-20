"""DEEP_PERFORMANCE wave 2 — caches de resolução (#DP042, #DP045, #DP046).

Foco: comportamento preservado + propriedade de performance (contagem de
chamadas via spy) onde barato.
"""

from __future__ import annotations

import json

import pytest

from alpha import approval, checkpoints
from alpha.agents import workspace as agents_ws


# ─── #DP042: memoização por CWD do path de settings.json ───


@pytest.fixture(autouse=True)
def _reset_permission_cache():
    approval.reset_permission_cache()
    yield
    approval.reset_permission_cache()


def _write_settings(root, deny=None, allow=None):
    d = root / ".alpha"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(
        json.dumps({"permissions": {"deny": deny or [], "allow": allow or []}}),
        encoding="utf-8",
    )


def test_dp042_rules_still_loaded_from_cwd_settings(tmp_path, monkeypatch):
    _write_settings(tmp_path, deny=["execute_shell:sudo"])
    monkeypatch.chdir(tmp_path)
    allow, deny = approval._load_permission_rules()
    assert any(r.tool == "execute_shell" for r in deny)


def test_dp042_cwd_change_rediscovers_settings(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write_settings(a, deny=["execute_shell:from_a"])
    _write_settings(b, deny=["execute_shell:from_b"])

    monkeypatch.chdir(a)
    _, deny_a = approval._load_permission_rules()
    monkeypatch.chdir(b)
    _, deny_b = approval._load_permission_rules()

    assert [r.raw for r in deny_a] == ["execute_shell:from_a"]
    assert [r.raw for r in deny_b] == ["execute_shell:from_b"]


def test_dp042_same_cwd_skips_find_config_file(tmp_path, monkeypatch):
    _write_settings(tmp_path, deny=["execute_shell:sudo"])
    monkeypatch.chdir(tmp_path)
    approval._load_permission_rules()  # semeia o cache de path

    calls = []
    real = approval.find_config_file

    def spy(name):
        calls.append(name)
        return real(name)

    monkeypatch.setattr(approval, "find_config_file", spy)
    for _ in range(5):
        approval._load_permission_rules()
    assert calls == []


def test_dp042_reset_clears_path_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    approval._load_permission_rules()  # sem settings → cacheia (cwd, None)
    _write_settings(tmp_path, deny=["execute_shell:late"])
    approval.reset_permission_cache()
    _, deny = approval._load_permission_rules()
    assert [r.raw for r in deny] == ["execute_shell:late"]


# ─── #DP045: memoização do root do workspace ───


def test_dp045_validate_args_behavior_preserved(tmp_path):
    agents_ws._resolved_ws.cache_clear()
    inside = tmp_path / "sub" / "f.txt"
    ok, new_args, err = agents_ws.validate_args(
        str(tmp_path), "read_file", {"path": str(inside)}
    )
    assert ok and err == ""
    assert new_args["path"] == str(inside.resolve())

    ok, _, err = agents_ws.validate_args(
        str(tmp_path), "read_file", {"path": "/etc/passwd"}
    )
    assert not ok
    assert "outside" in err


def test_dp045_root_resolved_once_per_workspace_string(tmp_path):
    agents_ws._resolved_ws.cache_clear()
    for i in range(4):
        agents_ws.validate_args(str(tmp_path), "read_file", {"path": f"f{i}.txt"})
    info = agents_ws._resolved_ws.cache_info()
    assert info.misses == 1
    assert info.hits == 3


def test_dp045_relative_paths_still_rewritten(tmp_path):
    agents_ws._resolved_ws.cache_clear()
    ok, new_args, _ = agents_ws.validate_args(
        str(tmp_path), "execute_shell", {"cwd": ""}
    )
    assert ok
    assert new_args["cwd"] == str(tmp_path.resolve())


# ─── #DP046: cache de contagem do index de checkpoints ───


@pytest.fixture()
def ckpt_ws(tmp_path, monkeypatch):
    monkeypatch.setattr("alpha.tools.workspace.AGENT_WORKSPACE", str(tmp_path))
    checkpoints._count_cache.clear()
    monkeypatch.setenv("ALPHA_CHECKPOINTS", "1")
    yield tmp_path
    checkpoints._count_cache.clear()


def test_dp046_snapshot_and_rewind_roundtrip(ckpt_ws):
    f = ckpt_ws / "x.txt"
    f.write_text("v1", encoding="utf-8")
    checkpoints.snapshot(f, "write_file")
    f.write_text("v2", encoding="utf-8")
    res = checkpoints.rewind(count=1)
    assert len(res["restored"]) == 1
    assert f.read_text(encoding="utf-8") == "v1"


def test_dp046_prune_still_caps_index(ckpt_ws, monkeypatch):
    monkeypatch.setenv("ALPHA_CHECKPOINT_LIMIT", "10")
    f = ckpt_ws / "x.txt"
    for i in range(65):  # limit(10) + 50 + margem
        f.write_text(f"v{i}", encoding="utf-8")
        checkpoints.snapshot(f, "write_file")
    d = checkpoints._ckpt_dir()
    entries = checkpoints._read_index(d)
    # a poda disparou (65 > 10+50) e o index nunca passa de limit+50+1
    assert len(entries) < 65
    assert len(entries) <= 10 + 50 + 1
    # blobs órfãos removidos junto
    blobs = list((d / checkpoints._BLOBS_DIR).glob("*.blob"))
    assert len(blobs) == len([e for e in entries if e.get("blob")])


def test_dp046_common_path_skips_index_reparse(ckpt_ws, monkeypatch):
    f = ckpt_ws / "x.txt"
    f.write_text("v0", encoding="utf-8")
    checkpoints.snapshot(f, "write_file")  # semeia o cache no prune

    reads = []
    real = checkpoints._read_index

    def spy(d):
        reads.append(str(d))
        return real(d)

    monkeypatch.setattr(checkpoints, "_read_index", spy)
    checkpoints.snapshot(f, "write_file")
    # _next_seq_locked ainda relê (contrato #204); o prune não relê mais →
    # exatamente 1 leitura no path comum, não 2.
    assert len(reads) == 1


def test_dp046_absent_cache_key_falls_back_to_disk(ckpt_ws, monkeypatch):
    f = ckpt_ws / "x.txt"
    f.write_text("v0", encoding="utf-8")
    checkpoints.snapshot(f, "write_file")
    # simula outro processo: cache por-processo perdido
    checkpoints._count_cache.clear()

    reads = []
    real = checkpoints._read_index

    def spy(d):
        reads.append(str(d))
        return real(d)

    monkeypatch.setattr(checkpoints, "_read_index", spy)
    f.write_text("v1", encoding="utf-8")
    checkpoints.snapshot(f, "write_file")
    # sem a chave, o prune volta a ler o disco (seq + prune = 2 leituras)
    assert len(reads) == 2
    # e re-semeia o cache com a contagem real
    d = checkpoints._ckpt_dir()
    assert checkpoints._count_cache[str(d)] == len(real(d))


def test_dp046_rewind_invalidates_count_cache(ckpt_ws):
    f = ckpt_ws / "x.txt"
    f.write_text("v1", encoding="utf-8")
    checkpoints.snapshot(f, "write_file")
    d = checkpoints._ckpt_dir()
    assert str(d) in checkpoints._count_cache
    checkpoints.rewind(count=1)
    assert str(d) not in checkpoints._count_cache
