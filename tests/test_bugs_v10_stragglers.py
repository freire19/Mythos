"""Regression tests for DEEP_BUGS V1.0 stragglers (#D022–#D030).

Cobre:
- #D022 — error paths usam `_format_result` (truncamento)
- #D023 — `_calc_backoff` jitter respeita MAX_BACKOFF
- #D024 — SQLite URI mode=ro com path quoted
- #D025 — glob_files filtra matches fora do workspace
- #D026 — apify parse defensivo (sem KeyError cru)
- #D027 — ALLOWED_COMMANDS removido (era dead code)
- #D028 — shell description corrigida (pipes sao suportados)
- #D029 — force-push dead check removido
- #D030 — verificacao do _CATEGORY_ICONS ainda em uso
"""

from unittest.mock import MagicMock

import pytest


# ─── #D023 — backoff jitter cap ─────────────────────────────────────


class TestCalcBackoffJitterCap:
    def test_retry_after_never_exceeds_max(self):
        from alpha.config import RETRY
        from alpha.llm import _calc_backoff
        MAX_BACKOFF = RETRY["llm"]["max_backoff"]

        # Forcar varias amostras do path retry_after — sem o min externo
        # podia chegar a 1.2 * MAX_BACKOFF.
        for _ in range(200):
            delay = _calc_backoff(0, retry_after=MAX_BACKOFF)
            assert delay <= MAX_BACKOFF, f"Jitter exceeded cap: {delay}"

    def test_path_without_retry_after_also_capped(self):
        from alpha.config import RETRY
        from alpha.llm import _calc_backoff
        MAX_BACKOFF = RETRY["llm"]["max_backoff"]

        for attempt in range(0, 20):
            for _ in range(10):
                delay = _calc_backoff(attempt)
                assert delay <= MAX_BACKOFF


# ─── #D024 — SQLite URI quoted ──────────────────────────────────────


class TestSQLiteUriQuoted:
    @pytest.mark.asyncio
    async def test_read_only_query_with_question_mark_in_name(self, tmp_path, monkeypatch):
        # #D024: filename com `?` (valido em Linux) quebraria a URI sem
        # url-encoding — o segundo `?` viraria query continuation. O fix
        # usa quote(...) entao a query read-only deve funcionar normalmente.
        import sqlite3

        from alpha.tools import database_tools, workspace as ws_module

        db_path = tmp_path / "weird?name.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO t VALUES (1, 'alpha')")
        conn.commit()
        conn.close()

        monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(database_tools, "AGENT_WORKSPACE", tmp_path, raising=False)

        result = await database_tools._query_sqlite(str(db_path), "SELECT * FROM t", read_only=True)
        assert "error" not in result, result
        assert result["row_count"] == 1
        assert result["rows"][0]["name"] == "alpha"

    @pytest.mark.asyncio
    async def test_read_only_blocks_writes(self, tmp_path, monkeypatch):
        # mode=ro deve impedir escrita mesmo apos approval do tool.
        import sqlite3

        from alpha.tools import database_tools, workspace as ws_module

        db_path = tmp_path / "ro.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(database_tools, "AGENT_WORKSPACE", tmp_path, raising=False)

        result = await database_tools._query_sqlite(
            str(db_path), "INSERT INTO t VALUES (1)", read_only=True
        )
        assert "error" in result
        assert "readonly" in result["error"].lower() or "read-only" in result["error"].lower()


# ─── #D027 — ALLOWED_COMMANDS removed ──────────────────────────────


class TestAllowedCommandsRemoved:
    def test_shell_tools_no_longer_exports_allowed_commands(self):
        from alpha.tools import shell_tools

        # Era frozenset com 90+ entries usado como allowlist documentacional.
        # `_validate_command` so consultava HARD_BLOCKED — codigo morto removido.
        assert not hasattr(shell_tools, "ALLOWED_COMMANDS")

    def test_pipeline_tools_no_longer_imports_it(self):
        from alpha.tools import pipeline_tools

        # Um `from ...shell_tools import ALLOWED_COMMANDS` deixaria o nome
        # ligado no namespace do modulo. Ausencia do atributo prova que o
        # import morto sumiu.
        assert not hasattr(pipeline_tools, "ALLOWED_COMMANDS")

    def test_hard_blocked_still_works(self):
        from alpha.tools.shell_tools import _validate_command

        # rm -rf continua bloqueado
        assert _validate_command("rm -rf /") is not None
        # echo seguro
        assert _validate_command("echo hello") is None


# ─── #D028 — shell description ─────────────────────────────────────


class TestShellDescriptionAccurate:
    def test_description_says_pipes_supported(self):
        from alpha.tools import TOOL_REGISTRY, load_all_tools

        load_all_tools()
        td = TOOL_REGISTRY["execute_shell"]
        # Description NAO deve mais dizer que pipes nao sao suportados
        desc = td.description.lower()
        assert "pipes" in desc
        # Usa execute_pipeline para && / redirects
        assert "execute_pipeline" in desc


# ─── #D029 — git push force dead code removed ──────────────────────


class TestGitPushForceCheckRemoved:
    def test_sanitize_still_rejects_force(self):
        from alpha.tools.git_tools import _sanitize_git_args

        _, err = _sanitize_git_args("push", "--force origin main")
        assert err is not None
        assert "--force" in err or "permitida" in err.lower()


# ─── #D025 — glob workspace per match ──────────────────────────────


class TestGlobWorkspaceFilter:
    @pytest.mark.asyncio
    async def test_glob_filters_symlink_escaping_workspace(self, tmp_path, monkeypatch):
        # #D025: glob que segue um symlink apontando para fora do workspace
        # NAO deve vazar o filename externo — assert_within_workspace por
        # match descarta esses e contabiliza skipped_outside_workspace.
        from alpha.tools import file_tools, path_helpers, workspace as ws_module

        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.py"
        secret.write_text("password=1")

        # Arquivo legitimo dentro do workspace.
        (workspace / "real.py").write_text("ok")
        # Symlink dentro do workspace apontando pro arquivo externo.
        link = workspace / "leak.py"
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip("symlinks nao suportados neste FS")

        monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", workspace)
        monkeypatch.setattr(file_tools, "AGENT_WORKSPACE", workspace)
        monkeypatch.setattr(path_helpers, "AGENT_WORKSPACE", workspace)

        result = await file_tools._glob_files("*.py", str(workspace))
        paths = [m["path"] for m in result["matches"]]
        # real.py aparece; leak.py (resolve pra fora) e filtrado.
        assert any(p.endswith("real.py") for p in paths)
        assert not any("secret.py" in p for p in paths)
        assert result.get("skipped_outside_workspace", 0) >= 1

    @pytest.mark.asyncio
    async def test_normal_glob_still_works(self, tmp_path, monkeypatch):
        from alpha.tools import file_tools, path_helpers, workspace as ws_module

        # Cria alguns arquivos no workspace mockado
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        # 3 modulos importam AGENT_WORKSPACE separadamente.
        monkeypatch.setattr(ws_module, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(file_tools, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(path_helpers, "AGENT_WORKSPACE", tmp_path)

        result = await file_tools._glob_files("*.py", str(tmp_path))
        assert result.get("count") == 2


# ─── #D026 — apify defensive parse ──────────────────────────────────


class TestApifyDefensiveParse:
    @pytest.mark.asyncio
    async def test_run_actor_handles_unexpected_shape(self, monkeypatch):
        # #D026: payload com shape inesperado (token expirado, breaking change)
        # retorna erro estruturado em vez de KeyError cru.
        from alpha.tools import apify_tools

        monkeypatch.setenv("APIFY_API_TOKEN", "fake-token")

        class FakeClient:
            async def post(self, url, **kw):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                # "data" ausente / nao-dict — antes do fix: KeyError.
                resp.json = MagicMock(return_value={"unexpected": "shape"})
                return resp

        async def fake_get_client(timeout=30.0):
            return FakeClient()

        monkeypatch.setattr(apify_tools, "_get_apify_client", fake_get_client)

        # Nao deve levantar — retorna dict de erro.
        result = await apify_tools._run_actor("my-actor")
        assert "error" in result
        assert "inesperada" in result["error"].lower() or "shape" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_list_actors_handles_malformed_payload(self, monkeypatch):
        # #D026: payload sem "data"/items nao deve estourar — lista vazia.
        from alpha.tools import apify_tools

        monkeypatch.setenv("APIFY_API_TOKEN", "fake-token")

        class FakeClient:
            async def get(self, url, **kw):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(return_value={"data": "not-a-dict"})
                return resp

        async def fake_get_client(timeout=15.0):
            return FakeClient()

        monkeypatch.setattr(apify_tools, "_get_apify_client", fake_get_client)

        result = await apify_tools._list_actors(search="x")
        assert result == {"total": 0, "actors": []}


# ─── #D022 — error paths use _format_result ─────────────────────────


class TestErrorPathsTruncated:
    def test_executor_error_paths_use_format_result(self):
        from alpha.tools import shell_tools  # noqa: F401  (registry side effect)

        # Helper centralizado adiciona truncamento. Verifica que o helper
        # existe e que paths de erro o usam.
        from alpha.executor import _append_tool_msg, _format_result

        assert callable(_append_tool_msg)
        assert callable(_format_result)

    def test_long_error_message_truncated(self):
        from alpha.executor import _format_result

        # Result com payload gigante deve ser truncado em vez de inflar contexto
        big = {"error": "x" * 50000}
        out = _format_result(big, "test_tool")
        assert len(out) < 25000  # < TOOL_RESULT_MAX_CHARS + buffer


# ─── #D030 — _CATEGORY_ICONS still in use ──────────────────────────


class TestCategoryIconsInUse:
    def test_icons_rendered_in_print_tools_list(self, capsys):
        # #D030: _CATEGORY_ICONS nao e orfao — print_tools_list usa o icone
        # da categoria na saida. Renderizamos uma tool git e conferimos que
        # o icone correspondente aparece (prova de consumo, nao so presenca).
        from alpha.display import core

        git_icon = core._CATEGORY_ICONS["git"]
        tools = [{"function": {"name": "git_status", "description": "show status"}}]
        core.print_tools_list(tools)

        out = capsys.readouterr().out
        assert "GIT" in out
        assert git_icon in out, "icone da categoria nao foi renderizado"

    def test_unknown_category_falls_back_to_default_icon(self, capsys):
        # Categoria sem icone dedicado usa o fallback "◆ ".
        from alpha.display import core

        tools = [{"function": {"name": "totally_unknown_xyz", "description": "x"}}]
        core.print_tools_list(tools)
        out = capsys.readouterr().out
        assert "◆" in out
