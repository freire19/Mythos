"""Checkpoints de arquivo + /rewind (gap nº1 vs Claude Code).

snapshot() antes de cada mutação, mark_turn() como fronteira, rewind()
restaura — turn-mode (default) ou count-mode (/rewind N).
"""

import json

import pytest

from alpha import checkpoints


@pytest.fixture(autouse=True)
def _ws(tmp_path, monkeypatch):
    """Workspace isolado por teste + flag ligada."""
    import alpha.tools.path_helpers as ph_mod
    import alpha.tools.workspace as ws_mod

    monkeypatch.setattr(ws_mod, "AGENT_WORKSPACE", tmp_path)
    # path_helpers importa o binding estaticamente — patch nos dois, como
    # fazem os demais testes de file tools (test_bugs_v10_stragglers etc.).
    monkeypatch.setattr(ph_mod, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.delenv("ALPHA_CHECKPOINTS", raising=False)
    monkeypatch.delenv("ALPHA_CHECKPOINT_LIMIT", raising=False)
    yield tmp_path


class TestSnapshotAndRewind:
    def test_restores_previous_content(self, _ws):
        f = _ws / "a.txt"
        f.write_text("v1")
        checkpoints.mark_turn("editar a")
        checkpoints.snapshot(f, "write_file")
        f.write_text("v2")

        result = checkpoints.rewind()
        assert f.read_text() == "v1"
        assert result["restored"]
        assert not result["skipped"]

    def test_new_file_rewind_deletes(self, _ws):
        f = _ws / "novo.txt"
        checkpoints.mark_turn("criar novo")
        checkpoints.snapshot(f, "write_file")  # não existia
        f.write_text("conteudo novo")

        checkpoints.rewind()
        assert not f.exists()

    def test_turn_mode_restores_whole_turn(self, _ws):
        a, b = _ws / "a.txt", _ws / "b.txt"
        a.write_text("a1")
        b.write_text("b1")

        checkpoints.mark_turn("turno 1")
        checkpoints.snapshot(a, "edit_file")
        a.write_text("a2")

        checkpoints.mark_turn("turno 2")
        checkpoints.snapshot(a, "edit_file")
        a.write_text("a3")
        checkpoints.snapshot(b, "write_file")
        b.write_text("b2")

        # /rewind → desfaz só o turno 2 (a3→a2, b2→b1)
        checkpoints.rewind()
        assert a.read_text() == "a2"
        assert b.read_text() == "b1"

        # /rewind de novo → desfaz o turno 1 (a2→a1)
        checkpoints.rewind()
        assert a.read_text() == "a1"

    def test_count_mode(self, _ws):
        f = _ws / "x.txt"
        f.write_text("v1")
        checkpoints.mark_turn("t")
        checkpoints.snapshot(f, "edit_file")
        f.write_text("v2")
        checkpoints.snapshot(f, "edit_file")
        f.write_text("v3")

        checkpoints.rewind(count=1)
        assert f.read_text() == "v2"
        checkpoints.rewind(count=1)
        assert f.read_text() == "v1"

    def test_same_file_multiple_edits_in_turn(self, _ws):
        f = _ws / "m.txt"
        f.write_text("original")
        checkpoints.mark_turn("t")
        checkpoints.snapshot(f, "edit_file")
        f.write_text("edit1")
        checkpoints.snapshot(f, "edit_file")
        f.write_text("edit2")

        checkpoints.rewind()
        assert f.read_text() == "original"

    def test_rewind_without_checkpoints(self, _ws):
        result = checkpoints.rewind()
        assert result["restored"] == []
        assert "Nenhum checkpoint" in result["message"]

    def test_binary_safe(self, _ws):
        f = _ws / "bin.dat"
        blob = bytes(range(256))
        f.write_bytes(blob)
        checkpoints.mark_turn("t")
        checkpoints.snapshot(f, "write_file")
        f.write_bytes(b"\x00\x01corrupted")

        checkpoints.rewind()
        assert f.read_bytes() == blob


class TestSafetyAndGating:
    def test_disabled_via_env(self, _ws, monkeypatch):
        monkeypatch.setenv("ALPHA_CHECKPOINTS", "0")
        f = _ws / "a.txt"
        f.write_text("v1")
        checkpoints.snapshot(f, "write_file")
        assert not (_ws / ".alpha" / "checkpoints").exists()

    def test_rewind_skips_paths_outside_workspace(self, _ws, tmp_path_factory):
        outside = tmp_path_factory.mktemp("fora") / "alvo.txt"
        outside.write_text("intacto")

        # Simula index adulterado apontando pra fora do workspace.
        d = _ws / ".alpha" / "checkpoints"
        (d / "blobs").mkdir(parents=True)
        (d / "blobs" / "1.blob").write_bytes(b"malicioso")
        (d / "index.jsonl").write_text(json.dumps({
            "seq": 1, "ts": 0.0, "kind": "edit", "tool": "write_file",
            "path": str(outside), "existed": True, "blob": "1.blob",
            "label": None,
        }) + "\n")

        result = checkpoints.rewind(count=1)
        assert outside.read_text() == "intacto"
        assert result["skipped"] and not result["restored"]

    def test_snapshot_never_raises(self, _ws, monkeypatch):
        # Storage quebrado (dir vira arquivo) → snapshot degrada pra log.
        (_ws / ".alpha").mkdir()
        (_ws / ".alpha" / "checkpoints").write_text("não sou um diretório")
        checkpoints.snapshot(_ws / "a.txt", "write_file")  # não pode levantar

    def test_consecutive_markers_collapse(self, _ws):
        checkpoints.mark_turn("primeiro")
        checkpoints.mark_turn("segundo")
        items = checkpoints.entries()
        markers = [e for e in items if e.get("kind") == "marker"]
        assert len(markers) == 1
        assert markers[0]["label"] == "segundo"

    def test_prune_respects_limit(self, _ws, monkeypatch):
        monkeypatch.setenv("ALPHA_CHECKPOINT_LIMIT", "10")
        f = _ws / "a.txt"
        f.write_text("x")
        for _ in range(70):
            checkpoints.snapshot(f, "edit_file")
        items = checkpoints.entries(limit=1000)
        assert len(items) <= 60  # 10 + folga de amortização (50)


class TestFileToolsIntegration:
    @pytest.mark.asyncio
    async def test_write_file_creates_checkpoint(self, _ws):
        from alpha.tools.file_tools import _write_file

        target = _ws / "doc.md"
        checkpoints.mark_turn("t")
        r1 = await _write_file(str(target), "versão 1")
        assert "error" not in r1
        r2 = await _write_file(str(target), "versão 2")
        assert "error" not in r2
        assert target.read_text() == "versão 2"

        checkpoints.rewind(count=1)
        assert target.read_text() == "versão 1"

    @pytest.mark.asyncio
    async def test_edit_file_creates_checkpoint(self, _ws):
        from alpha.tools.file_tools import _edit_file, _write_file

        target = _ws / "code.py"
        await _write_file(str(target), "x = 1\ny = 2\n")
        checkpoints.mark_turn("t")
        r = await _edit_file(str(target), "x = 1", "x = 99")
        assert "error" not in r
        assert "x = 99" in target.read_text()

        checkpoints.rewind()
        assert "x = 1" in target.read_text()


class TestCrossProcessSeq:
    """AUDIT_V2.3 #204: alocação de seq relê o disco sob flock — um outro
    processo (sub-agent background) avançando o index não pode colidir."""

    def test_next_seq_rereads_disk_not_cache(self, _ws, tmp_path):
        from alpha import checkpoints as cp

        f = tmp_path / "a.txt"
        f.write_text("v1")
        cp.snapshot(f, "write_file")  # seq 1

        # Simula OUTRO processo appendando seq=10 no mesmo index.
        d = tmp_path / ".alpha" / "checkpoints"
        cp._append(d, {
            "seq": 10, "ts": 0.0, "kind": "edit", "tool": "write_file",
            "path": str(f), "existed": True, "blob": None, "label": None,
        })

        f.write_text("v2")
        cp.snapshot(f, "write_file")  # deve alocar 11, não 2
        entries = cp._read_index(d)
        assert entries[-1]["seq"] == 11

    def test_blob_name_carries_pid_and_regex_accepts_both(self, _ws, tmp_path):
        import os as _os

        from alpha import checkpoints as cp

        f = tmp_path / "b.txt"
        f.write_text("conteudo")
        cp.snapshot(f, "edit_file")
        d = tmp_path / ".alpha" / "checkpoints"
        entry = cp._read_index(d)[-1]
        assert entry["blob"] == f"1.{_os.getpid()}.blob"
        assert cp._BLOB_NAME_RE.match(entry["blob"])
        assert cp._BLOB_NAME_RE.match("42.blob")  # legado
        assert not cp._BLOB_NAME_RE.match("../42.blob")
        assert not cp._BLOB_NAME_RE.match("42.blob/x")

    def test_rewind_restores_pid_suffixed_blob(self, _ws, tmp_path):
        from alpha import checkpoints as cp

        f = tmp_path / "c.txt"
        f.write_text("original")
        cp.mark_turn("turno")
        f2 = tmp_path / "c.txt"
        cp.snapshot(f2, "write_file")
        f2.write_text("modificado")
        out = cp.rewind()
        assert out["restored"], out
        assert f2.read_text() == "original"
