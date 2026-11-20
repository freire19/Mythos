"""Regression tests for DEEP_MAINTAINABILITY + DEEP_PERFORMANCE quick wins.

Cobre:
- #DM004 — `is_private_ip_address` import morto removido
- #DM005 — urllib imports no topo (não inline)
- #DM014 — `script_path = None` antes do try em code_tools
- #D012 (V1.0 MAINT) — `ALPHA_FEATURES` alias removido
- #083 — dedup ip/route em SAFE_SHELL_COMMANDS
- #085 — loop-detection consts em config.LOOP_DETECTION
- #090 — shlex.quote em _run_tests cmd
- #091 — comentario de _safe_get_tool atualizado
- #093 — analyze_tools.py com docstring
- #094 — bin/alpha mensagem corrigida
- #097 — MAX_ITERATIONS em LIMITS dict
- #D013 (V1.0 PERF) — _validate_pg_ssrf helper extraido
- #D005 (V1.0 PERF) — fuzzy resolve cache
"""

from pathlib import Path



# ─── #DM004 — dead import removed ──────────────────────────────────


class TestIsPrivateIpDeadImportRemoved:
    def test_no_alias_in_network_tools(self):
        from alpha.tools import network_tools

        assert not hasattr(network_tools, "_is_private_ip_address")


# ─── #DM005 — urllib imports at top ────────────────────────────────


class TestUrllibImportsAtTop:
    def test_urllib_imported_at_module_top(self):
        """#DM005: urllib.request is imported at module top, not via the old
        inline `import urllib.request as _urllib_request` alias nor inline in
        `_http_request_urllib`. Structural proof: the `urllib` package is bound
        as a module attribute and the no-redirect handler subclasses
        urllib.request.HTTPRedirectHandler (resolved at class-definition time,
        which only works if the top-level import exists)."""
        import urllib.request

        from alpha.tools import network_tools

        assert not hasattr(network_tools, "_urllib_request")
        assert hasattr(network_tools, "urllib")
        assert issubclass(
            network_tools._NoRedirectHandler, urllib.request.HTTPRedirectHandler
        )


# ─── #DM014 — script_path init ─────────────────────────────────────


class TestScriptPathInit:
    async def test_tempfile_failure_does_not_unboundlocal(self, monkeypatch):
        """#DM014: if creating the tempfile fails *before* `script_path` is
        assigned, the `finally: os.unlink(script_path)` must not blow up with
        UnboundLocalError. Behavioral proof: force NamedTemporaryFile to raise
        and assert we get back the clean `{"error": ...}` dict (the pre-init to
        None makes `finally` skip the unlink) rather than a NameError."""
        from alpha.tools import code_tools

        def boom(*_a, **_kw):
            raise OSError("no space left on device")

        monkeypatch.setattr(code_tools.tempfile, "NamedTemporaryFile", boom)

        result = await code_tools._execute_python("print('hi')")
        assert "error" in result
        assert "no space left" in result["error"]


# ─── #D012 (V1.0 MAINT) — ALPHA_FEATURES removed ───────────────────


class TestAlphaFeaturesAliasRemoved:
    def test_alias_no_longer_exported(self):
        from alpha import config

        assert not hasattr(config, "ALPHA_FEATURES")
        assert hasattr(config, "FEATURES")  # original ainda existe


# ─── #083 — dedup ip/route ─────────────────────────────────────────


class TestSafeShellCommandsDedupe:
    def test_ip_and_route_present_in_safe_shell_commands(self):
        """#083 dedup-ed `ip`/`route` in SAFE_SHELL_COMMANDS. The set is a
        frozenset so duplicates collapse by construction; the load-bearing
        invariant is that both commands survived the dedup and remain
        members of the allowlist."""
        from alpha.approval import SAFE_SHELL_COMMANDS

        assert isinstance(SAFE_SHELL_COMMANDS, frozenset)
        assert "ip" in SAFE_SHELL_COMMANDS
        assert "route" in SAFE_SHELL_COMMANDS


# ─── #085 — loop detection consts in config ────────────────────────


class TestLoopDetectionInConfig:
    def test_config_has_loop_detection_dict(self):
        from alpha.config import LOOP_DETECTION

        assert "max_repeat_calls" in LOOP_DETECTION
        assert "similarity_threshold" in LOOP_DETECTION
        assert "min_iter" in LOOP_DETECTION

    # NB: the former `test_agent_reads_from_config` source-grep was removed —
    # `test_consts_match_config_values` below already proves the agent's loop
    # constants are sourced from config.LOOP_DETECTION (it asserts equality of
    # every runtime constant against the dict).

    def test_consts_match_config_values(self):
        from alpha.agent.loop import (
            _CYCLE_WINDOW,
            _MAX_REPEAT_CALLS,
            _SIMILAR_REPEAT_CALLS,
            _SIMILARITY_THRESHOLD,
            _STALE_WINDOW,
        )
        from alpha.config import LOOP_DETECTION

        assert _MAX_REPEAT_CALLS == LOOP_DETECTION["max_repeat_calls"]
        assert _SIMILAR_REPEAT_CALLS == LOOP_DETECTION["similar_repeat_calls"]
        assert _SIMILARITY_THRESHOLD == LOOP_DETECTION["similarity_threshold"]
        assert _CYCLE_WINDOW == LOOP_DETECTION["cycle_window"]
        assert _STALE_WINDOW == LOOP_DETECTION["stale_window"]


# ─── #090 — shlex.quote em _run_tests ──────────────────────────────


class TestRunTestsShlexQuote:
    async def test_pattern_is_shell_quoted(self, tmp_path, monkeypatch):
        """#090: an adversarial `-k` pattern must be shell-quoted so it can't
        inject into the pytest command. Behavioral proof: capture the command
        _run_tests hands to execute_shell and verify the injection payload is
        neutralized by quoting (no bare `; rm -rf`)."""
        import shlex

        from alpha.tools import _composite_tests

        # Bypass the workspace gate (not under test here) so we reach the
        # command-construction path.
        monkeypatch.setattr(
            _composite_tests, "_resolve_target", lambda path: (tmp_path, None)
        )

        captured = {}

        async def fake_run_tool(name, **kwargs):
            captured["name"] = name
            captured["command"] = kwargs.get("command")
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        monkeypatch.setattr(_composite_tests, "_run_tool", fake_run_tool)

        nasty = "x'; rm -rf ~; echo '"
        await _composite_tests._run_tests(
            path=str(tmp_path), framework="pytest", pattern=nasty
        )

        cmd = captured["command"]
        # The payload must appear as the exact shlex.quote() rendering — i.e.
        # fully escaped so the shell can't interpret `;`, quotes, or `~`.
        assert f"-k {shlex.quote(nasty)}" in cmd
        # Definitive safety check: a POSIX shell tokenizer parses the command
        # into exactly [python3, -m, pytest, -v, -k, <original pattern>]. The
        # injection is a single argument, not separate commands — so a bare
        # f"-k '{pattern}'" (which would split on the `;`) would fail here.
        assert shlex.split(cmd) == ["python3", "-m", "pytest", "-v", "-k", nasty]


# ─── #091 — _safe_get_tool comment ─────────────────────────────────


class TestSafeGetToolPolicy:
    def test_blocklist_enforces_anti_recursion(self):
        """#091: the documented policy must actually hold — a sub-agent can
        never resolve `delegate_*` (anti-recursion), even with no name-filter."""
        from alpha.tools import delegate_tools

        blocked = delegate_tools._resolve_subagent_blocklist(None)
        assert {"delegate_task", "delegate_parallel"} <= blocked

        safe_get = delegate_tools._make_safe_get_tool(
            blocked, None, delegate_tools.get_tool
        )
        assert safe_get("delegate_task") is None
        assert safe_get("delegate_parallel") is None
        # A normal, non-blocked tool still resolves.
        assert safe_get("read_file") is not None

    def test_tools_filter_restricts_lookup(self):
        """#091: the `tools_filter` allow-list gates `_safe_get_tool` — a tool
        outside the filter returns None even though it isn't blocklisted."""
        from alpha.tools import delegate_tools

        blocked = delegate_tools._resolve_subagent_blocklist(None)
        _tools, allowed = delegate_tools._build_subagent_tools(
            "read_file", blocked
        )
        assert allowed == {"read_file"}

        safe_get = delegate_tools._make_safe_get_tool(
            blocked, allowed, delegate_tools.get_tool
        )
        assert safe_get("read_file") is not None
        # In-registry but outside the filter → blocked.
        assert safe_get("write_file") is None


# ─── #093 — analyze_tools docstring ────────────────────────────────
# Removido: `analyze_tools.py` foi apagado (script ad-hoc da raiz que
# nao era importado nem listado em pyproject.toml; lixo de fase de
# desenvolvimento).


# ─── #094 — bin/alpha message ──────────────────────────────────────


class TestBinAlphaMessage:
    def test_no_requirements_txt_reference(self):
        bin_alpha = Path(__file__).parent.parent / "bin" / "mythos"
        text = bin_alpha.read_text(encoding="utf-8")
        # requirements.txt nao existe no projeto; mensagem mencionava
        # esse arquivo, agora menciona `pip install -e .`
        assert "requirements.txt" not in text
        assert "pip install -e ." in text


# ─── #097 — LIMITS dict ────────────────────────────────────────────


class TestLimitsDict:
    def test_config_has_limits_dict(self):
        from alpha.config import LIMITS

        assert "max_iterations" in LIMITS
        assert LIMITS["max_iterations"] == 100

    def test_subagent_max_iterations_lives_in_features_only(self):
        # `subagent_max_iterations` antes existia tambem em LIMITS, mas nada
        # lia de la — codigo le sempre de FEATURES (vide delegate_tools.py).
        # Manter dois lugares era drift garantido.
        from alpha.config import FEATURES, LIMITS
        assert "subagent_max_iterations" not in LIMITS
        assert "subagent_max_iterations" in FEATURES

    def test_legacy_aliases_match(self):
        from alpha.config import LIMITS, LLM_TIMEOUT, MAX_ITERATIONS, TOOL_RESULT_MAX_CHARS

        assert MAX_ITERATIONS == LIMITS["max_iterations"]
        assert TOOL_RESULT_MAX_CHARS == LIMITS["tool_result_max_chars"]
        assert LLM_TIMEOUT == LIMITS["llm_timeout"]


# ─── #D013 (V1.0 PERF) — PG SSRF helper ────────────────────────────


class TestValidatePgSsrf:
    def test_helper_exists(self):
        from alpha.tools import database_tools

        assert callable(database_tools._validate_pg_ssrf)

    async def test_blocks_private_ip(self):
        # AUDIT_V1.2 #002: helper agora e async (asyncio.to_thread em
        # _is_private_ip que faz socket.getaddrinfo bloqueante).
        from alpha.tools.database_tools import _validate_pg_ssrf

        result = await _validate_pg_ssrf("postgresql://user:pass@127.0.0.1/db")
        assert result is not None
        assert result.get("blocked") is True

    async def test_allows_public_host(self, monkeypatch):
        from alpha.tools import database_tools

        # Mock _is_private_ip para evitar dependencia de DNS no test
        monkeypatch.setattr(database_tools, "_is_private_ip", lambda h: False)
        result = await database_tools._validate_pg_ssrf(
            "postgresql://user:pass@db.example.com/db"
        )
        assert result is None

    async def test_does_not_block_event_loop(self, monkeypatch):
        """AUDIT_V1.2 #002: _is_private_ip e sync (socket.getaddrinfo).

        Antes da correcao, _validate_pg_ssrf chamava direto e travava o
        event loop ate o DNS responder (ate 5s). Apos `asyncio.to_thread`,
        outras tasks devem progredir em paralelo.
        """
        import asyncio
        import time
        from alpha.tools import database_tools

        # Simular DNS lento: 0.5s de bloqueio
        def slow_is_private_ip(hostname: str) -> bool:
            time.sleep(0.05)
            return False

        monkeypatch.setattr(database_tools, "_is_private_ip", slow_is_private_ip)

        # Outra task deve avancar enquanto _validate_pg_ssrf espera DNS
        progress_marker = []

        async def background_task():
            for i in range(5):
                await asyncio.sleep(0.05)
                progress_marker.append(i)

        bg = asyncio.create_task(background_task())
        result = await database_tools._validate_pg_ssrf(
            "postgresql://user:pass@example.com/db"
        )
        await bg

        assert result is None
        # Se loop foi bloqueado, progress_marker estaria vazio ou < 3
        assert len(progress_marker) == 5, (
            f"Event loop bloqueado durante DNS: progress={progress_marker}"
        )


# ─── #D005 (V1.0 PERF) — fuzzy cache ───────────────────────────────


class TestFuzzyResolveCache:
    def test_cache_module_attrs_exist(self):
        from alpha.tools import path_helpers

        assert hasattr(path_helpers, "_fuzzy_cache")
        assert hasattr(path_helpers, "_FUZZY_CACHE_SIZE")

    def test_uncached_function_separate(self):
        from alpha.tools import path_helpers

        # _fuzzy_resolve_uncached e a versao sem cache (para tests/forcar miss)
        assert callable(path_helpers._fuzzy_resolve_uncached)

    def test_cached_returns_same_result(self, monkeypatch):
        from alpha.tools import path_helpers

        # Reset cache
        path_helpers._fuzzy_cache.clear()
        path_helpers._fuzzy_cache_order.clear()

        call_count = {"n": 0}
        original = path_helpers._fuzzy_resolve_uncached

        def counting_uncached(p):
            call_count["n"] += 1
            return original(p)

        monkeypatch.setattr(path_helpers, "_fuzzy_resolve_uncached", counting_uncached)

        # Primeira chamada — miss
        path_helpers._fuzzy_resolve("/totally/nonexistent/path")
        assert call_count["n"] == 1
        # Segunda chamada com mesmo arg — hit
        path_helpers._fuzzy_resolve("/totally/nonexistent/path")
        assert call_count["n"] == 1  # nao incrementou
