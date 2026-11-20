"""Wave 2 de manutenibilidade — higiene dos módulos de tool (#DM062/#DM063/#DM073/#DM078).

Garante o comportamento preservado (e o único behavior change mandatado:
stderr do git alinhado ao STDERR_CAP global) após a centralização dos caps
de truncagem e a interpolação dos timeouts reais nos schemas LLM-facing.
"""

from __future__ import annotations

import inspect

import pytest

from alpha.config import TOOL_TIMEOUTS, TOOL_TIMEOUT_CAPS
from alpha.tools._subprocess_helpers import (
    STDERR_CAP,
    STDOUT_CAP,
    SubprocessResult,
)


# ─── #DM062: caps únicos ───


def test_caps_canonicos():
    """Os valores canônicos são os que sempre estiveram em vigor (15000/5000)."""
    assert STDOUT_CAP == 15000
    assert STDERR_CAP == 5000


def _fake_result(monkeypatch, module, stdout: bytes, stderr: bytes):
    async def fake_run(*cmd, **kwargs):
        return SubprocessResult(0, stdout, stderr)

    monkeypatch.setattr(module, "run_subprocess_safe", fake_run)


@pytest.mark.asyncio
async def test_git_stderr_alinhado_ao_cap_global(monkeypatch):
    """#DM062: git usava 3000 divergente; agora corta stderr em STDERR_CAP."""
    from alpha.tools import git_tools

    _fake_result(monkeypatch, git_tools, b"o" * 20000, b"e" * 20000)
    result = await git_tools._run_git(["status"], cwd=".")
    assert len(result["stdout"]) == STDOUT_CAP
    assert len(result["stderr"]) == STDERR_CAP


@pytest.mark.asyncio
async def test_execute_python_caps_preservados(monkeypatch):
    from alpha.tools import code_tools

    _fake_result(monkeypatch, code_tools, b"o" * 20000, b"e" * 20000)
    result = await code_tools._execute_python("print('x')")
    assert len(result["stdout"]) == STDOUT_CAP
    assert len(result["stderr"]) == STDERR_CAP


# ─── #DM063: schemas LLM-facing dizem a verdade ───


def _timeout_schema(tool_name: str) -> dict:
    from alpha.tools import get_openai_tools, load_all_tools

    load_all_tools()
    for tool in get_openai_tools():
        if tool["function"]["name"] == tool_name:
            return tool["function"]["parameters"]["properties"]["timeout"]
    raise AssertionError(f"tool {tool_name} não registrada")


@pytest.mark.parametrize(
    "tool_name,category",
    [
        ("execute_shell", "shell"),
        ("execute_pipeline", "pipeline"),
        ("execute_python", "code"),
    ],
)
def test_timeout_schema_reflete_config(tool_name, category):
    schema = _timeout_schema(tool_name)
    effective_default = min(TOOL_TIMEOUTS[category], TOOL_TIMEOUT_CAPS[category])
    assert schema["default"] == effective_default
    assert f"Default: {effective_default}" in schema["description"]
    assert f"max {TOOL_TIMEOUT_CAPS[category]}" in schema["description"]


# ─── #DM073: símbolos mortos removidos ───


def test_dead_symbols_removidos():
    from alpha.tools import database_tools, pipeline_tools

    assert not hasattr(database_tools, "_MAX_RESULT_CHARS")
    assert not hasattr(pipeline_tools, "_PIPE_OPERATORS")


# ─── #DM078: assinaturas sem implicit-Optional ───


def test_sem_implicit_optional_nas_assinaturas():
    from alpha.tools import (
        _composite_deploy,
        _composite_project,
        _composite_tests,
        git_tools,
        network_tools,
        pipeline_tools,
        shell_tools,
    )

    funcs = [
        shell_tools._execute_shell,
        pipeline_tools._execute_pipeline,
        network_tools._http_request,
        network_tools._http_request_with_retry,
        git_tools._git_operation,
        _composite_tests._run_tests,
        _composite_deploy._deploy_check,
        _composite_project._project_overview,
    ]
    for func in funcs:
        sig = inspect.signature(func)
        for param in sig.parameters.values():
            if param.default is None and param.annotation != inspect.Parameter.empty:
                assert "None" in str(param.annotation), (
                    f"{func.__qualname__}.{param.name}: {param.annotation!r} "
                    "com default None deve ser explicit-Optional (X | None)"
                )
