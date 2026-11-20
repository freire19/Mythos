"""DEEP_RESILIENCE wave 2 — #D808 (fsync/atomicidade do index), #D820
(encoding utf-8 no I/O do index) e #D821 (ordem do atexit em lifecycle)."""

from __future__ import annotations

import json
import os
from unittest import mock

from alpha import checkpoints


def _entry(seq: int, path: str = "/tmp/x") -> dict:
    return {
        "seq": seq, "ts": 1.0, "kind": "edit", "tool": "write_file",
        "path": path, "existed": False, "blob": None, "label": None,
    }


# ---------------------------------------------------------------- #D808


class TestWriteIndexAtomic:
    def test_write_index_fsyncs_before_replace(self, tmp_path):
        calls = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=spy_fsync), \
                mock.patch("os.replace", side_effect=spy_replace):
            checkpoints._write_index(tmp_path, [_entry(1)])

        assert "fsync" in calls
        assert calls.index("fsync") < calls.index("replace")

    def test_write_index_uses_unique_tmp_and_cleans_up(self, tmp_path):
        checkpoints._write_index(tmp_path, [_entry(1), _entry(2)])
        leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []
        idx = tmp_path / checkpoints._INDEX_NAME
        lines = idx.read_text(encoding="utf-8").splitlines()
        assert [json.loads(li)["seq"] for li in lines] == [1, 2]

    def test_cross_process_lock_creates_lockfile(self, tmp_path):
        if checkpoints.fcntl is None:
            return  # Windows: degrada para lock por-processo
        with checkpoints._cross_process_lock(tmp_path):
            assert (tmp_path / ".lock").exists()


# ---------------------------------------------------------------- #D820


class TestIndexEncoding:
    def test_roundtrip_non_ascii_under_c_locale(self, tmp_path):
        entry = _entry(1, path="/tmp/ação/café.py")
        # Simula LANG=C: open de texto sem encoding= cairia em ASCII.
        with mock.patch("locale.getpreferredencoding", return_value="ascii"):
            checkpoints._write_index(tmp_path, [entry])
            out = checkpoints._read_index(tmp_path)
        assert out == [entry]
        raw = (tmp_path / checkpoints._INDEX_NAME).read_bytes()
        assert "café".encode("utf-8") in raw

    def test_read_index_passes_utf8_encoding(self, tmp_path):
        entry = _entry(3, path="/tmp/coração.txt")
        checkpoints._write_index(tmp_path, [entry])
        seen = {}
        real_read_text = type(tmp_path).read_text

        def spy_read_text(self, *args, **kwargs):
            seen["encoding"] = kwargs.get("encoding")
            return real_read_text(self, *args, **kwargs)

        with mock.patch.object(type(tmp_path), "read_text", spy_read_text):
            out = checkpoints._read_index(tmp_path)
        assert seen["encoding"] == "utf-8"
        assert out == [entry]


# ---------------------------------------------------------------- #D821


class TestAtexitOrder:
    def test_on_stop_registered_last_fires_first(self, monkeypatch):
        from alpha.cli import lifecycle

        monkeypatch.setattr(lifecycle, "_INSTALLED", False)
        monkeypatch.setattr(lifecycle, "_install_sigterm_handler", lambda: None)
        registered = []
        monkeypatch.setattr(
            lifecycle.atexit, "register", lambda fn: registered.append(fn)
        )
        lifecycle.install_lifecycle_hooks()

        assert registered, "nenhum hook registrado"
        # LIFO: o último registrado roda primeiro → on_stop deve ser o último.
        assert registered[-1] is lifecycle._fire_on_stop
        # Ordem relativa dos demais preservada (indicator logo após on_stop).
        names = [getattr(f, "__name__", str(f)) for f in registered]
        assert names.index("cleanup_indicator") == len(names) - 2
        assert names.index("_shutdown_browser_session") < names.index(
            "_shutdown_mcp_servers"
        ) < names.index("_shutdown_pg_pools")

    def test_install_is_idempotent(self, monkeypatch):
        from alpha.cli import lifecycle

        monkeypatch.setattr(lifecycle, "_INSTALLED", False)
        monkeypatch.setattr(lifecycle, "_install_sigterm_handler", lambda: None)
        registered = []
        monkeypatch.setattr(
            lifecycle.atexit, "register", lambda fn: registered.append(fn)
        )
        lifecycle.install_lifecycle_hooks()
        n = len(registered)
        lifecycle.install_lifecycle_hooks()
        assert len(registered) == n
