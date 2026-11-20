"""run_tests tool — composite (#030 split)."""

from __future__ import annotations

import asyncio
import os
import shlex

from ..executor import _annotate_error
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _resolve_target, _run_tool
from .file_tools import _SKIP_DIRS


def _has_test_files(root) -> bool:
    for _dirpath, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            if f.endswith(".py") and (f.startswith("test_") or f.endswith("_test.py")):
                return True
    return False


async def _run_tests(
    path: str | None = None,
    framework: str = "auto",
    pattern: str | None = None,
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
            if await asyncio.to_thread(_has_test_files, target_path):
                framework = "pytest"
            else:
                # #017: passar pelo `_annotate_error` (como o ramo de framework
                # não-suportado em :69) para manter o invariante {ok:false,
                # category} que o trust-model #D110 espera — o modelo vê falha
                # de tool num formato único em vez de um `{error}` cru.
                return _annotate_error(
                    {"error": "Nao foi possivel detectar o framework de testes automaticamente. Especifique 'framework'."},
                    "runtime",
                )

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
            # AUDIT_V1.8 #005: sem shlex.quote, `pattern="x; rm -rf ~"` virava
            # shell injection apos o user aprovar `run_tests` (composite skipa
            # approval em sub-tools). pytest ja usava shlex.quote (linha 50);
            # cargo/go ficavam vulneraveis.
            cmd += f" {shlex.quote(pattern)}"
    elif framework == "go":
        cmd = "go test ./..."
        if pattern:
            # AUDIT_V1.8 #005: single-quote literal nao escapa `'` dentro do
            # pattern adversarial (`pattern="x'; rm -rf ~; echo '"`). shlex.quote
            # produz o quoting POSIX-correto.
            cmd += f" -run {shlex.quote(pattern)}"
    else:
        return _annotate_error(
            {"error": f"Framework '{framework}' nao suportado. Use: pytest, npm, cargo, go"},
            "runtime",
        )

    result = await _run_tool("execute_shell", command=cmd, cwd=str(target_path), timeout=120)
    # AUDIT_V2.2 #010: only stamp framework/command on a real test result —
    # don't pollute an _annotate_error dict (timeout/denied/runtime) with keys
    # that make a failed sub-tool look like a test that actually ran.
    if not result.get("error") and result.get("ok") is not False:
        result["framework"] = framework
        result["command"] = cmd
    return result


register_tool(
    ToolDefinition(
        name="run_tests",
        description=(
            "Detect the test framework and run it. Supports pytest, npm test, cargo test, go test. "
            "Auto-detection based on the project's configuration files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project path (optional, uses the default workspace)",
                },
                "framework": {
                    "type": "string",
                    "description": "Test framework. 'auto' to detect automatically",
                    "enum": ["auto", "pytest", "npm", "cargo", "go"],
                    "default": "auto",
                },
                "pattern": {
                    "type": "string",
                    "description": "Pattern to filter specific tests (e.g. 'test_auth' for pytest)",
                },
            },
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.COMPOSITE,
        executor=_run_tests,
    )
)
