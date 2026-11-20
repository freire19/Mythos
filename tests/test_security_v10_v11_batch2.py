"""Regression tests for DEEP_SECURITY V1.0/V1.1 batch 2.

Cobre:
- #027 — dead `_BLOCKED_IMPORT_RE` regex removida
- #028 — safe_env cache TTL refresh
- #022 — sub-agent task_content nao vaza absolute workspace path
- #030 — multi-statement SQL detector handles SQL standard `''` escape
"""


import pytest


# ─── #027 — dead code removed ──────────────────────────────────────


class TestDeadRegexRemoved:
    def test_blocked_import_re_no_longer_module_level(self):
        from alpha.tools import code_tools

        # AST-based validation only (#D018-PERF + #027). A regex era 75 linhas
        # de codigo morto que duplicava o blocklist.
        assert not hasattr(code_tools, "_BLOCKED_IMPORT_RE")
        assert not hasattr(code_tools, "_BLOCKED_IMPORT_PATTERNS")

    def test_ast_validation_still_blocks_pickle(self):
        from alpha.tools.code_tools import _validate_code_safety

        err = _validate_code_safety("import pickle\nx = pickle.loads(b'')")
        assert err is not None

    def test_ast_validation_blocks_runpy(self):
        from alpha.tools.code_tools import _validate_code_safety

        err = _validate_code_safety("from runpy import run_path")
        assert err is not None

    def test_open_blocked_with_clean_message_not_legacy_regex(self):
        # Historicamente o regex legacy `\bopen\s*\(.*(w|a|x)` casava em
        # `open("read.txt")` por causa do "a" em "read" — false positive.
        # DEEP_SECURITY V3.3 #D124: agora bloqueamos TODO `open()` (mesmo
        # modo "r") por design, com mensagem clara. Este test garante que
        # o bloqueio venha do check explicito do AST (mensagem mencionando
        # `read_file`/`write_file`), nao de uma regressao do regex legacy.
        from alpha.tools.code_tools import _validate_code_safety

        err = _validate_code_safety('open("read.txt")')
        assert err is not None
        assert "read_file" in err or "write_file" in err, (
            f"Expected new V3.3 message, got: {err}"
        )

        err = _validate_code_safety('open("read.txt", "r")')
        assert err is not None
        assert "read_file" in err or "write_file" in err


# ─── #028 — safe_env TTL ───────────────────────────────────────────


class TestSafeEnvCache:
    # TTL-based invalidation (#028) was replaced by len(os.environ) check
    # (#036) — the two TTL tests went away with it. invalidate_safe_env_cache
    # is still the explicit reset hook and still works.

    def test_invalidate_forces_immediate_refresh(self, monkeypatch):
        from alpha.tools import safe_env

        safe_env.invalidate_safe_env_cache()
        safe_env.get_safe_env()
        monkeypatch.setenv("ALPHA_TEST_INVALIDATE", "ok")
        safe_env.invalidate_safe_env_cache()
        assert safe_env.get_safe_env().get("ALPHA_TEST_INVALIDATE") == "ok"


# ─── #022 — workspace path not leaked to sub-agent ─────────────────


class TestSubagentPathPrivacy:
    def test_task_content_uses_relative_scratch_not_absolute_workspace(self):
        """#022: o prompt do sub-agent recebe o SCRATCH_DIR RELATIVO, e o path
        absoluto do workspace NAO deve vazar no conteudo da mensagem (ficava
        acessivel via tool results e logs). Exercitamos o builder diretamente."""
        from pathlib import Path

        from alpha.tools.delegate_tools import _build_subagent_messages

        abs_workspace = "/home/secret-user/private-workspace"
        scratch_rel = Path(".alpha/runs/agent-xyz")

        messages = _build_subagent_messages(
            task="faca a tarefa",
            context="algum contexto",
            agent_id="agent-xyz",
            scratch_rel=scratch_rel,
        )
        user_msg = next(m for m in messages if m["role"] == "user")["content"]

        # O scratch relativo aparece; o workspace absoluto NAO.
        assert str(scratch_rel) in user_msg
        assert abs_workspace not in user_msg
        # Sanity: nao ha preâmbulo `[CWD: <absoluto>]` legado.
        assert "[CWD:" not in user_msg

    def test_run_subagent_passes_relative_scratch_to_builder(self, monkeypatch):
        """Integra: `_run_subagent` deriva o scratch RELATIVO ao workspace e o
        repassa ao builder — capturamos o argumento `scratch_rel`."""
        import asyncio
        from pathlib import Path

        from alpha.tools import delegate_tools

        captured = {}

        def fake_builder(task, context, agent_id, scratch_rel, system_extra=""):
            captured["scratch_rel"] = scratch_rel
            # Interrompe cedo para evitar rodar o agent de verdade.
            raise RuntimeError("stop-after-build")

        monkeypatch.setattr(delegate_tools, "_build_subagent_messages", fake_builder)

        async def go():
            return await delegate_tools._run_subagent(
                task="t", parent_workspace="/tmp/parent-ws"
            )

        # O builder e chamado ANTES do try interno, entao o sentinel propaga.
        with pytest.raises(RuntimeError, match="stop-after-build"):
            asyncio.run(go())

        rel = captured["scratch_rel"]
        # scratch_rel e RELATIVO (nao comeca com /), e fica sob .alpha/runs.
        assert not Path(rel).is_absolute()
        assert ".alpha" in str(rel) and "runs" in str(rel)


# ─── #030 — SQL standard escape ────────────────────────────────────


class TestSQLStandardEscape:
    def test_doubled_quote_inside_string_no_false_block(self):
        from alpha.tools.database_tools import _is_dangerous_query

        # 'O''Brien' e string valida com apostrofe escapado por dobramento.
        # Nao deve bloquear (sem multi-statement real).
        assert _is_dangerous_query("SELECT 'O''Brien' AS name") is None

    def test_real_multistatement_blocked(self):
        from alpha.tools.database_tools import _is_dangerous_query

        result = _is_dangerous_query("SELECT 1; DROP TABLE users")
        assert result is not None
        assert "multi-statement" in result.lower()

    def test_backslash_quote_no_longer_treated_as_escape(self):
        # SQL standard: backslash NAO escapa quote. O detector legacy
        # tratava `\'` como escape, criando bypass:
        # `SELECT 'a\'; DROP TABLE t; --` parecia 1 statement mas em
        # SQL standard sao 2.
        from alpha.tools.database_tools import _is_dangerous_query

        result = _is_dangerous_query("SELECT 'a\\'; DROP TABLE t; --")
        assert result is not None
        assert "multi-statement" in result.lower()

    def test_semicolon_inside_doubled_quote_string_ok(self):
        from alpha.tools.database_tools import _is_dangerous_query

        # ';' literal dentro de string SQL valida — nao e multi-statement
        assert _is_dangerous_query("SELECT 'value; with; semicolons' AS x") is None

    def test_trailing_semicolon_with_only_comment_allowed(self):
        from alpha.tools.database_tools import _is_dangerous_query

        # Trailing comment apos `;` nao e multi-statement
        assert _is_dangerous_query("SELECT 1; -- trailing comment") is None
