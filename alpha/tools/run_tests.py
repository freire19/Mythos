"""Run tests — composite tool for test framework detection and execution."""

import logging
import os
import shlex
from pathlib import Path

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _annotate_error, _run_tool, _violation
from .path_helpers import _validate_path
from .workspace import AGENT_WORKSPACE

logger = logging.getLogger(__name__)


async def _run_tests(
    path: str = None,
    framework: str = "auto",
    pattern: str = None,
) -> dict:
    """Detect test framework and run tests."""
    target = path or str(AGENT_WORKSPACE)
    try:
        target_path = _validate_path(target)
    except PermissionError as e:
        return _violation(str(e))

    # Auto-detect framework
    if framework == "auto":
        if (target_path / "pytest.ini").exists() or (target_path / "pyproject.toml").exists():
            framework = "pytest"
        elif (target_path / "package.json").exists():
            framework = "npm"
        elif (target_path / "Cargo.toml").exists():
            framework = "cargo"
        elif (target_path / "go.mod").exists():
            framework = "go"
        else:
            # #D028: use os.walk with skip-dirs instead of rglob
            # to avoid traversing .git/.venv/node_modules/__pycache__.
            _SKIP_TEST_DIRS = {
                ".git", "node_modules", ".venv", "__pycache__",
                ".mypy_cache", ".pytest_cache", ".ruff_cache",
                "dist", "build", ".tox",
            }
            test_files = []
            for dirpath, dirs, files in os.walk(str(target_path)):
                dirs[:] = [d for d in dirs if d not in _SKIP_TEST_DIRS and not d.startswith(".")]
                for fname in files:
                    if (fname.startswith("test_") or fname.endswith("_test.py")) and fname.endswith(".py"):
                        test_files.append(Path(dirpath) / fname)
                        if len(test_files) >= 5:  # early exit: we only need to know they EXIST
                            break
                if len(test_files) >= 5:
                    break
            if test_files:
                framework = "pytest"
            else:
                return {
                    "error": "Nao foi possivel detectar o framework de testes "
                    "automaticamente. Especifique 'framework'."
                }

    # Build command based on framework
    if framework == "pytest":
        cmd = "python3 -m pytest -v"
        if pattern:
            cmd += f" -k {shlex.quote(pattern)}"
    elif framework == "npm":
        cmd = "npm test"
    elif framework == "cargo":
        cmd = "cargo test"
        if pattern:
            cmd += f" {pattern}"
    elif framework == "go":
        cmd = "go test ./..."
        if pattern:
            cmd += f" -run '{pattern}'"
    else:
        return _annotate_error(
            {"error": f"Framework '{framework}' nao suportado. Use: pytest, npm, cargo, go"},
            "runtime",
        )

    # Execute via shell tool
    result = await _run_tool("execute_shell", command=cmd, cwd=str(target_path), timeout=120)

    result["framework"] = framework
    result["command"] = cmd
    return result


register_tool(
    ToolDefinition(
        name="run_tests",
        description=(
            "Detectar framework de testes e executar. Suporta pytest, npm test, "
            "cargo test, go test. Auto-detecao baseada em arquivos de configuracao "
            "do projeto."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Caminho do projeto (opcional, usa workspace padrao)",
                },
                "framework": {
                    "type": "string",
                    "description": "Framework de testes. 'auto' para detectar automaticamente",
                    "enum": ["auto", "pytest", "npm", "cargo", "go"],
                    "default": "auto",
                },
                "pattern": {
                    "type": "string",
                    "description": "Padrao para filtrar testes especificos (ex: 'test_auth' para pytest)",
                },
            },
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.COMPOSITE,
        executor=_run_tests,
    )
)
