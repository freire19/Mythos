"""run_tests tool — composite (#030 split)."""

from __future__ import annotations

import shlex

from ..executor import _annotate_error
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _resolve_target, _run_tool
from .file_tools import _SKIP_DIRS


async def _run_tests(
    path: str = None,
    framework: str = "auto",
    pattern: str = None,
) -> dict:
    """Detect test framework and run tests."""
    target_path, err = _resolve_target(path)
    if err:
        return err

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
            test_files = [
                p for p in target_path.rglob("*.py")
                if (p.name.startswith("test_") or p.name.endswith("_test.py"))
                and not any(part in _SKIP_DIRS for part in p.relative_to(target_path).parts)
            ]
            if test_files:
                framework = "pytest"
            else:
                return {
                    "error": "Nao foi possivel detectar o framework de testes automaticamente. Especifique 'framework'."
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

    result = await _run_tool("execute_shell", command=cmd, cwd=str(target_path), timeout=120)
    result["framework"] = framework
    result["command"] = cmd
    return result


register_tool(
    ToolDefinition(
        name="run_tests",
        description=(
            "Detectar framework de testes e executar. Suporta pytest, npm test, cargo test, go test. "
            "Auto-detecao baseada em arquivos de configuracao do projeto."
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
