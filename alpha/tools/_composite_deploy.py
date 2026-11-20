"""deploy_check tool — composite (#030 split)."""

from __future__ import annotations

import asyncio

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ._composite_helpers import _resolve_target, _run_tool
from ._composite_tests import _run_tests


async def _deploy_check(path: str | None = None) -> dict:
    """Run pre-deployment checklist: tests, git status, lint."""
    target_path, err = _resolve_target(path)
    if err:
        return err

    checks = {}

    tasks = {
        "git_status": _run_tool("git_operation", action="status", path=str(target_path)),
        "tests": _run_tests(path=str(target_path)),
    }

    gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)

    all_passed = True
    for key, result in zip(tasks.keys(), gathered):
        if isinstance(result, Exception):
            checks[key] = {"status": "error", "error": str(result)}
            all_passed = False
        elif "error" in result:
            checks[key] = {"status": "error", **result}
            all_passed = False
        else:
            # AUDIT_V1.10 #021: `exit_code` default 0 tratava tools que não
            # expõem o campo como "pass" — falso positivo de deploy-ready.
            # Agora: se tem exit_code explícito, usa-o; senão busca ok/success;
            # se nada, marca "unknown" (não conta como pass nem fail).
            if "exit_code" in result:
                exit_code = int(result["exit_code"])
                checks[key] = {"status": "pass" if exit_code == 0 else "fail", **result}
                if exit_code != 0:
                    all_passed = False
            else:
                ok = result.get("ok") or result.get("success")
                if ok is True:
                    checks[key] = {"status": "pass", **result}
                elif ok is False:
                    checks[key] = {"status": "fail", **result}
                    all_passed = False
                else:
                    # #014: um check que não expõe exit_code/ok não pode ser
                    # tratado como "pass" — um deploy-gate deve ser conservador.
                    # Status "unknown" agora reprova all_passed (não bloqueia o
                    # relatório, mas impede o "Pronto para deploy" falso).
                    checks[key] = {"status": "unknown", "note": "tool não expõe exit_code/ok", **result}
                    all_passed = False

    return {
        "all_passed": all_passed,
        "checks": checks,
        "path": str(target_path),
        "recommendation": (
            "Pronto para deploy" if all_passed
            else "Corrija os problemas antes de fazer deploy"
        ),
    }


register_tool(
    ToolDefinition(
        name="deploy_check",
        description=(
            "Run a pre-deploy checklist: git status, tests, and general checks. "
            "Returns whether it's ready to deploy or which problems to fix."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project path (optional, uses the default workspace)",
                },
            },
        },
        # AUDIT_V2.2 #005: deploy_check invokes _run_tests → execute_shell
        # (npm test / pytest / cargo test) via _run_tool, which skips the
        # approval gate (#D110). A SAFE composite that runs arbitrary project
        # test scripts un-prompted violates the trust model — run_tests itself
        # is DESTRUCTIVE for the same reason. Mark DESTRUCTIVE so the user
        # approves before any test script runs.
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.COMPOSITE,
        executor=_deploy_check,
    )
)
