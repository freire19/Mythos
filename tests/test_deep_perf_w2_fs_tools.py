"""Regression tests for the DEEP_PERFORMANCE wave-2 fs-tools fixes.

- #DP028: search_and_replace roda preview e apply fora do event loop
  (asyncio.to_thread), preservando contagem real, guarda de search vazio
  e escrita atomica.
- #DP029: glob_files recursivo usa os.walk com poda REAL de _SKIP_DIRS
  (nao desce em node_modules/.venv), em to_thread, mantendo cap de 200,
  guarda #DB18 e o filtro de workspace (#D025).
- #DP030: auto-detect de run_tests usa walk podado com early-exit em vez
  de materializar rglob("*.py") inteiro.
- #DP040: list_directory roda iterdir+stat em to_thread e nao stat-a
  entradas alem das 300 retornadas (count continua sendo o total).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from alpha.tools import _composite_snr as snr
from alpha.tools import _composite_tests as ct
from alpha.tools import file_tools, path_helpers
from alpha.tools import workspace as ws_module


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(file_tools, "AGENT_WORKSPACE", tmp_path)
    monkeypatch.setattr(path_helpers, "AGENT_WORKSPACE", tmp_path)
    return tmp_path


def _thread_spy(monkeypatch, module, name, calls):
    """Wrap module.<name> recording the thread it executes on."""
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        calls.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)


# ─── #DP028 — search_and_replace ───────────────────────────────────


class TestDP028SearchAndReplace:
    def _stub_search(self, monkeypatch, workspace, files):
        async def fake_run_tool(tool, **kwargs):
            return {"results": [{"file": str(f)} for f in files]}

        monkeypatch.setattr(snr, "_run_tool", fake_run_tool)
        monkeypatch.setattr(
            snr, "_resolve_target", lambda p, **kw: (workspace, None)
        )
        monkeypatch.setattr("alpha.checkpoints.snapshot", lambda *a, **kw: None)
        monkeypatch.setattr(
            snr, "_validate_path_no_symlink", lambda fp: Path(fp)
        )

    async def test_dry_run_counts_real_occurrences_off_loop(
        self, workspace, monkeypatch
    ):
        a = workspace / "a.txt"
        b = workspace / "b.txt"
        a.write_text("foo foo foo\n")
        b.write_text("x foo y\n")
        self._stub_search(monkeypatch, workspace, [a, b])

        threads: list[threading.Thread] = []
        _thread_spy(monkeypatch, snr, "_snr_preview_sync", threads)

        result = await snr._search_and_replace(
            path=str(workspace), search="foo", replace="bar", dry_run=True
        )

        assert result["dry_run"] is True
        assert result["files_affected"] == 2
        assert result["total_matches"] == 4
        assert result["files"] == {str(a): 3, str(b): 1}
        # Preview correu fora da MainThread (asyncio.to_thread).
        assert threads and all(
            t is not threading.main_thread() for t in threads
        )

    async def test_apply_replaces_and_reports_off_loop(
        self, workspace, monkeypatch
    ):
        a = workspace / "a.txt"
        b = workspace / "b.txt"
        a.write_text("foo foo\n")
        b.write_text("nothing here\n")
        self._stub_search(monkeypatch, workspace, [a, b])

        threads: list[threading.Thread] = []
        _thread_spy(monkeypatch, snr, "_snr_apply_sync", threads)

        result = await snr._search_and_replace(
            path=str(workspace), search="foo", replace="bar", dry_run=False
        )

        assert result["dry_run"] is False
        assert result["files_changed"] == 1
        assert result["total_replacements"] == 2
        assert result["changed"] == [{"file": str(a), "replacements": 2}]
        assert result["errors"] is None
        assert a.read_text() == "bar bar\n"
        assert b.read_text() == "nothing here\n"
        assert threads and all(
            t is not threading.main_thread() for t in threads
        )

    async def test_apply_reports_missing_file_error(
        self, workspace, monkeypatch
    ):
        ghost = workspace / "ghost.txt"
        self._stub_search(monkeypatch, workspace, [ghost])

        result = await snr._search_and_replace(
            path=str(workspace), search="foo", replace="bar", dry_run=False
        )
        assert result["files_changed"] == 0
        assert result["errors"] == [
            {"file": str(ghost), "error": "Arquivo nao encontrado"}
        ]

    async def test_empty_search_still_rejected(self, workspace):
        # Guarda #DB15 preservada.
        result = await snr._search_and_replace(
            path=str(workspace), search="", replace="x", dry_run=False
        )
        assert "error" in result


# ─── #DP029 — glob_files ───────────────────────────────────────────


class TestDP029GlobFiles:
    def _tree(self, workspace):
        (workspace / "src" / "pkg").mkdir(parents=True)
        (workspace / "src" / "a.py").write_text("")
        (workspace / "src" / "pkg" / "b.py").write_text("")
        (workspace / "top.py").write_text("")
        (workspace / "node_modules" / "deep").mkdir(parents=True)
        (workspace / "node_modules" / "deep" / "c.py").write_text("")
        (workspace / ".venv" / "lib").mkdir(parents=True)
        (workspace / ".venv" / "lib" / "d.py").write_text("")

    async def test_recursive_glob_matches_and_skips_noise(self, workspace):
        self._tree(workspace)
        result = await file_tools._glob_files("**/*.py", str(workspace))
        rels = sorted(
            str(Path(m["path"]).relative_to(workspace))
            for m in result["matches"]
        )
        assert rels == ["src/a.py", "src/pkg/b.py", "top.py"]
        assert result["count"] == 3

    async def test_recursive_glob_prunes_skip_dirs_and_offloads(
        self, workspace, monkeypatch
    ):
        self._tree(workspace)
        visited: list[str] = []
        threads: list[threading.Thread] = []
        real_walk = os.walk

        def spy_walk(top, *args, **kwargs):
            for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
                threads.append(threading.current_thread())
                visited.append(dirpath)
                yield dirpath, dirnames, filenames

        monkeypatch.setattr(os, "walk", spy_walk)
        await file_tools._glob_files("**/*.py", str(workspace))

        # Poda REAL: a traversal nunca entra em node_modules/.venv.
        assert visited
        assert not any("node_modules" in v or ".venv" in v for v in visited)
        assert all(t is not threading.main_thread() for t in threads)

    async def test_recursive_glob_includes_dirs_like_pathlib(self, workspace):
        self._tree(workspace)
        result = await file_tools._glob_files("**/*", str(workspace))
        by_rel = {
            str(Path(m["path"]).relative_to(workspace)): m["type"]
            for m in result["matches"]
        }
        assert by_rel["src"] == "dir"
        assert by_rel["src/pkg"] == "dir"
        assert by_rel["src/a.py"] == "file"
        assert "node_modules" not in by_rel
        assert ".venv" not in by_rel

    async def test_recursive_cap_of_200_counts_real_matches(self, workspace):
        for i in range(230):
            (workspace / f"f{i:03d}.txt").write_text("")
        result = await file_tools._glob_files("**/*.txt", str(workspace))
        assert result["count"] == 200
        assert len(result["matches"]) == 200

    async def test_db18_guards_preserved(self, workspace):
        # Absoluto (com **) e vazio continuam retornando erro acionavel.
        result = await file_tools._glob_files(
            str(workspace / "**" / "*.py"), str(workspace)
        )
        assert "error" in result
        result = await file_tools._glob_files("", str(workspace))
        assert "error" in result

    async def test_symlink_outside_workspace_still_skipped(self, workspace):
        # #D025 preservado no walker novo.
        (workspace / "ok.py").write_text("")
        (workspace / "escape.py").symlink_to("/etc/hostname")
        result = await file_tools._glob_files("**/*.py", str(workspace))
        paths = [m["path"] for m in result["matches"]]
        assert str(workspace / "ok.py") in paths
        assert str(workspace / "escape.py") not in paths
        assert result["skipped_outside_workspace"] == 1

    async def test_non_recursive_glob_still_works_off_loop(
        self, workspace, monkeypatch
    ):
        (workspace / "a.py").write_text("")
        (workspace / "b.txt").write_text("")
        threads: list[threading.Thread] = []
        _thread_spy(monkeypatch, file_tools, "_glob_flat_sync", threads)

        result = await file_tools._glob_files("*.py", str(workspace))
        assert result["count"] == 1
        assert result["matches"][0]["path"] == str(workspace / "a.py")
        assert threads and all(
            t is not threading.main_thread() for t in threads
        )


# ─── #DP030 — run_tests auto-detect ────────────────────────────────


class TestDP030RunTestsDetect:
    async def test_detects_test_files_without_config(
        self, tmp_path, monkeypatch
    ):
        async def fake_run_tool(name, **kw):
            return {"stdout": "", "command": kw.get("command")}

        monkeypatch.setattr(ct, "_run_tool", fake_run_tool)
        monkeypatch.setattr(ct, "_resolve_target", lambda p, **k: (tmp_path, None))

        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "foo_test.py").write_text("def test_x(): pass\n")
        res = await ct._run_tests(str(tmp_path))
        assert res.get("framework") == "pytest"

    async def test_skip_dirs_pruned_in_detection(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ct, "_resolve_target", lambda p, **k: (tmp_path, None))
        (tmp_path / ".venv" / "lib").mkdir(parents=True)
        (tmp_path / ".venv" / "lib" / "test_vendor.py").write_text("")
        res = await ct._run_tests(str(tmp_path))
        assert res.get("ok") is False and "error" in res

    def test_has_test_files_early_exits(self, tmp_path, monkeypatch):
        (tmp_path / "test_a.py").write_text("")
        deep = tmp_path / "z_deep"
        for i in range(5):
            deep = deep / f"lvl{i}"
        deep.mkdir(parents=True)
        (deep / "test_b.py").write_text("")

        consumed: list[str] = []
        real_walk = os.walk

        def spy_walk(top, *args, **kwargs):
            for item in real_walk(top, *args, **kwargs):
                consumed.append(item[0])
                yield item

        monkeypatch.setattr(os, "walk", spy_walk)
        assert ct._has_test_files(tmp_path) is True
        # Early-exit: encontrou no primeiro nivel, nao desceu na arvore.
        assert consumed == [str(tmp_path)]

    def test_has_test_files_false_when_none(self, tmp_path):
        (tmp_path / "app.py").write_text("")
        assert ct._has_test_files(tmp_path) is False


# ─── #DP040 — list_directory ───────────────────────────────────────


class TestDP040ListDirectory:
    async def test_listing_shape_preserved_off_loop(
        self, workspace, monkeypatch
    ):
        (workspace / "adir").mkdir()
        (workspace / "b.txt").write_text("hello")
        threads: list[threading.Thread] = []
        _thread_spy(monkeypatch, file_tools, "_list_directory_sync", threads)

        result = await file_tools._list_directory(str(workspace))
        assert result["path"] == str(workspace)
        assert result["count"] == 2
        assert result["entries"] == [
            {"name": "adir", "type": "dir"},
            {"name": "b.txt", "type": "file", "size": 5},
        ]
        assert threads and all(
            t is not threading.main_thread() for t in threads
        )

    async def test_count_is_total_but_entries_capped_at_300(self, workspace):
        for i in range(320):
            (workspace / f"f{i:03d}").write_text("")
        result = await file_tools._list_directory(str(workspace))
        assert result["count"] == 320
        assert len(result["entries"]) == 300

    async def test_no_stat_beyond_cap(self, workspace, monkeypatch):
        for i in range(320):
            (workspace / f"f{i:03d}").write_text("")
        statted: list[str] = []
        real_stat = Path.stat

        def spy_stat(self, *args, **kwargs):
            statted.append(self.name)
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", spy_stat)
        await file_tools._list_directory(str(workspace))
        # Entradas alem das 300 retornadas (f300..f319, ordenadas por nome)
        # nao sofrem is_dir/is_file/stat.
        beyond = {f"f{i:03d}" for i in range(300, 320)}
        assert not beyond.intersection(statted)
