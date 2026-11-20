"""Coverage para consolidacao de TOOL_TIMEOUTS (#D003 V1.0 MAINT).

Antes:
- `executor.TOOL_EXECUTION_TIMEOUT=120` e `_SLOW_TOOL_TIMEOUT=300` viviam
  em `executor.py` (independentes de config).
- Hard caps inline: shell_tools=300, code_tools=60, network_tools=60,
  pipeline_tools=120 — divergentes da config.

Agora:
- `config.TOOL_EXECUTION_TIMEOUT`, `config.SLOW_TOOL_TIMEOUT` sao a fonte.
- `config.TOOL_TIMEOUT_CAPS` substitui os literais inline.
- `executor.py` re-exporta por retro-compat (composite_tools, tests).
"""

from __future__ import annotations

import asyncio

import pytest


class TestConfigCentralization:
    def test_executor_timeouts_in_config(self):
        from alpha import config

        assert hasattr(config, "TOOL_EXECUTION_TIMEOUT")
        assert hasattr(config, "SLOW_TOOL_TIMEOUT")
        assert config.TOOL_EXECUTION_TIMEOUT == 120
        assert config.SLOW_TOOL_TIMEOUT == 300

    def test_caps_in_config(self):
        from alpha.config import TOOL_TIMEOUT_CAPS

        # Todas as categorias com hard cap conhecida
        assert TOOL_TIMEOUT_CAPS["shell"] == 300
        assert TOOL_TIMEOUT_CAPS["code"] == 60
        assert TOOL_TIMEOUT_CAPS["network"] == 60
        assert TOOL_TIMEOUT_CAPS["pipeline"] == 120

    def test_executor_reexports_for_compat(self):
        """`composite_tools` e tests legacy importam de executor — manter."""
        from alpha import executor

        assert executor.TOOL_EXECUTION_TIMEOUT == 120
        assert executor._SLOW_TOOL_TIMEOUT == 300


class TestToolFilesUseConfigCaps:
    """Behavioral: cada tool DEVE limitar o timeout pedido ao cap da config.

    Em vez de grepar o source por `TOOL_TIMEOUT_CAPS`, chamamos a tool com um
    timeout absurdamente grande e capturamos o timeout efetivo propagado para o
    subprocess/wait_for. Se a regressao reintroduzir um literal divergente (ou
    remover o cap), o valor capturado diverge da config e o teste falha.
    """

    @pytest.mark.asyncio
    async def test_shell_caps_timeout_to_config(self, monkeypatch, tmp_path):
        from alpha.tools import shell_tools
        from alpha.config import TOOL_TIMEOUT_CAPS

        captured = {}

        async def fake_run(*cmd, timeout=30, **kwargs):
            captured["timeout"] = timeout
            raise shell_tools.SubprocessTimeoutError()

        monkeypatch.setattr(shell_tools, "run_subprocess_safe", fake_run)
        monkeypatch.setattr(shell_tools, "AGENT_WORKSPACE", tmp_path)

        # cwd omitido -> usa shell_tools.AGENT_WORKSPACE (patchado).
        await shell_tools._execute_shell("echo hi", timeout=99999)
        assert captured["timeout"] == TOOL_TIMEOUT_CAPS["shell"]

    @pytest.mark.asyncio
    async def test_code_caps_timeout_to_config(self, monkeypatch):
        from alpha.tools import code_tools
        from alpha.config import TOOL_TIMEOUT_CAPS

        captured = {}

        async def fake_run(*cmd, timeout=30, **kwargs):
            captured["timeout"] = timeout
            raise code_tools.SubprocessTimeoutError()

        monkeypatch.setattr(code_tools, "run_subprocess_safe", fake_run)

        # `print(1)` passa pelo validador de seguranca AST.
        await code_tools._execute_python("print(1)", timeout=99999)
        assert captured["timeout"] == TOOL_TIMEOUT_CAPS["code"]

    @pytest.mark.asyncio
    async def test_network_caps_timeout_to_config(self, monkeypatch):
        from alpha.tools import network_tools
        from alpha.config import TOOL_TIMEOUT_CAPS

        captured = {}

        async def fake_wait_for(coro, timeout):
            captured["timeout"] = timeout
            # fecha a coroutine pra evitar warning de "never awaited"
            if asyncio.iscoroutine(coro):
                coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(network_tools.asyncio, "wait_for", fake_wait_for)

        # URL publica valida (passa SSRF), GET — entra no caminho do wait_for.
        await network_tools._http_request("https://example.com", timeout=99999)
        # _http_request usa wait_for(..., timeout=timeout + 5); o cap esta no
        # `timeout` base, entao o efetivo e cap + 5.
        assert captured["timeout"] == TOOL_TIMEOUT_CAPS["network"] + 5

    @pytest.mark.asyncio
    async def test_pipeline_caps_timeout_to_config(self, monkeypatch, tmp_path):
        from alpha.tools import pipeline_tools
        from alpha.config import TOOL_TIMEOUT_CAPS

        captured = {}

        async def fake_wait_for(coro, timeout):
            captured.setdefault("timeout", timeout)
            if asyncio.iscoroutine(coro):
                coro.close()
            raise asyncio.TimeoutError()

        class _FakeProc:
            returncode = None

            async def communicate(self, input=None):
                return b"", b""

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def fake_exec(*a, **k):
            return _FakeProc()

        monkeypatch.setattr(pipeline_tools, "AGENT_WORKSPACE", tmp_path)
        monkeypatch.setattr(pipeline_tools.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(pipeline_tools.asyncio, "wait_for", fake_wait_for)

        # cwd omitido -> usa pipeline_tools.AGENT_WORKSPACE (patchado).
        await pipeline_tools._execute_pipeline("echo hi", timeout=99999)
        # A primeira chamada de wait_for envolve proc.communicate(..., timeout=timeout).
        assert captured["timeout"] == TOOL_TIMEOUT_CAPS["pipeline"]


class TestExecutorReadFromConfig:
    """O executor deve usar os MESMOS objetos que a config expoe, nao copias
    com literais hardcoded. Comparamos identidade de valor com a config."""

    def test_executor_values_match_config(self):
        from alpha import config, executor

        assert executor.TOOL_EXECUTION_TIMEOUT == config.TOOL_EXECUTION_TIMEOUT
        assert executor._SLOW_TOOL_TIMEOUT == config.SLOW_TOOL_TIMEOUT

    def test_slow_tools_get_slow_timeout(self):
        """Sanity: a logica de selecao usa SLOW_TOOL_TIMEOUT para slow tools
        e o default para o resto — distintos."""
        from alpha import executor

        assert executor._SLOW_TOOL_TIMEOUT != executor.TOOL_EXECUTION_TIMEOUT
        assert executor._SLOW_TOOLS  # nao vazio
