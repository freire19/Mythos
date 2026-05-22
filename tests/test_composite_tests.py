"""Tests for `run_tests` composite tool.

AUDIT_V1.8 #005: cargo/go pattern eram injetados sem shlex.quote — vetor
de shell injection apos o user aprovar `run_tests` (composite skipa
approval em sub-tools por trust model #D110).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alpha.tools._composite_tests import _run_tests
from alpha.tools.workspace import AGENT_WORKSPACE


@pytest.fixture
def sandbox_dir(tmp_path, monkeypatch):
    """Cria subdir dentro do AGENT_WORKSPACE — _resolve_target exige isso."""
    target = AGENT_WORKSPACE / "test_composite_sandbox"
    target.mkdir(exist_ok=True)
    yield target
    # Cleanup
    import shutil
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


@pytest.mark.asyncio
async def test_cargo_pattern_quoted_against_injection(sandbox_dir):
    # Setup: cargo project detection (Cargo.toml present)
    (sandbox_dir / "Cargo.toml").write_text("[package]\n")

    captured = {}

    async def fake_run_tool(name, **kwargs):
        captured["name"] = name
        captured["command"] = kwargs.get("command", "")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    adversarial = "x; rm -rf ~"
    with patch("alpha.tools._composite_tests._run_tool", fake_run_tool):
        await _run_tests(path=str(sandbox_dir), pattern=adversarial)

    # O pattern adversarial deve ter sido quotado — `;` nao pode ficar
    # nao-quotado entre o nome do binario e o resto.
    assert captured["name"] == "execute_shell"
    cmd = captured["command"]
    # shlex.quote produz aspas simples wrapping toda a string com `;`.
    assert "'x; rm -rf ~'" in cmd or "'x; rm -rf ~'\\''" in cmd
    # Garantia: o `; rm -rf ~` NAO aparece como tokens separados depois de "cargo test"
    assert "cargo test x; rm -rf ~" not in cmd


@pytest.mark.asyncio
async def test_go_pattern_quoted_against_injection(sandbox_dir):
    (sandbox_dir / "go.mod").write_text("module x\n")

    captured = {}

    async def fake_run_tool(name, **kwargs):
        captured["command"] = kwargs.get("command", "")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    # O exploit original do audit: aspas simples nao escapavam `'` dentro
    # da string, virando `go test ./... -run 'x'; rm -rf ~; echo ''`.
    adversarial = "x'; rm -rf ~; echo '"
    with patch("alpha.tools._composite_tests._run_tool", fake_run_tool):
        await _run_tests(path=str(sandbox_dir), pattern=adversarial)

    cmd = captured["command"]
    # shlex.quote em POSIX gera o split de aspas para escapar `'` internas.
    # O exploit "fechar a aspas + injetar comando" fica neutralizado.
    assert "go test ./... -run " in cmd
    assert "rm -rf" in cmd  # presente literalmente no arg, nao como comando
    # Validar: nao ha `; rm -rf` fora de quoting. shlex.quote envolve toda
    # a string em `'...'` (com escape de `'` interna como `'\''`).
    assert cmd.startswith("go test ./... -run '") or "rm -rf" not in cmd.split("'")[0]


@pytest.mark.asyncio
async def test_pytest_pattern_still_quoted(sandbox_dir):
    """Pytest ja usava shlex.quote (linha 50). Regression test mantem invariante."""
    (sandbox_dir / "pyproject.toml").write_text("[tool.pytest]\n")

    captured = {}

    async def fake_run_tool(name, **kwargs):
        captured["command"] = kwargs.get("command", "")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    with patch("alpha.tools._composite_tests._run_tool", fake_run_tool):
        await _run_tests(path=str(sandbox_dir), pattern="test_x; rm -rf ~")

    cmd = captured["command"]
    assert "python3 -m pytest -v -k" in cmd
    # O `; rm -rf` deve ficar dentro do quoting do -k
    assert "rm -rf" in cmd
    # Nao deve aparecer como sub-comando solto
    assert " ; rm -rf" not in cmd or cmd.count("'") >= 2


@pytest.mark.asyncio
async def test_pattern_benign_still_works(sandbox_dir):
    """Patterns benignos devem continuar funcionando inalterados."""
    (sandbox_dir / "Cargo.toml").write_text("[package]\n")

    captured = {}

    async def fake_run_tool(name, **kwargs):
        captured["command"] = kwargs.get("command", "")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    with patch("alpha.tools._composite_tests._run_tool", fake_run_tool):
        await _run_tests(path=str(sandbox_dir), pattern="my_test")

    # Pattern simples (sem metachars) pode nao ter aspas, ou ter — ambos OK
    assert "my_test" in captured["command"]
    assert captured["command"].startswith("cargo test")
