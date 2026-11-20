"""Shared helpers for composite tools.

Extracted from composite_tools.py (#030 split)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..executor import (
    TOOL_EXECUTION_TIMEOUT,
    _SLOW_TOOL_TIMEOUT,
    _SLOW_TOOLS,
    _annotate_error,
)
from . import get_tool
from .path_helpers import _validate_path
from .workspace import AGENT_WORKSPACE

logger = logging.getLogger(__name__)


def _violation(msg: str) -> dict:
    """Workspace-violation result with the executor's standard error invariant."""
    return _annotate_error({"error": msg, "workspace_violation": True}, "violation")


def _resolve_target(
    path: str | None, *, default_to_workspace: bool = True
) -> tuple[Path | None, dict | None]:
    """Resolve and validate a composite tool's target path.

    Centraliza o padrao `target = path or AGENT_WORKSPACE; _validate_path; _violation`
    que era duplicado em todas as 4 composite tools (#DM025).

    Returns:
        (Path, None) on success — caller can use the resolved path.
        (None, violation_dict) on failure — caller should `return` it directly.

    Args:
        path: Caminho a resolver. Se None/vazio e default_to_workspace=True,
            usa AGENT_WORKSPACE. Se default_to_workspace=False, retorna
            violation quando path estiver ausente (e.g. search_and_replace
            que exige path explicito).
    """
    if not path:
        if not default_to_workspace:
            return None, _violation("path e obrigatorio para esta tool")
        path = str(AGENT_WORKSPACE)
    try:
        return _validate_path(path), None
    except PermissionError as e:
        return None, _violation(str(e))


async def _run_tool(name: str, *, timeout: float | None = None, **kwargs) -> dict:
    """Execute a registered tool by name.

    Adiciona enforcement de timeout (TOOL_EXECUTION_TIMEOUT por default,
    _SLOW_TOOL_TIMEOUT para tools registradas como slow). Sem isso, sub-tools
    da composite hangam indefinidamente — o timeout do agent so corta apos
    o cap do composite (300s), nao o do sub-tool.

    TRUST MODEL (#D110): este helper invoca tools internas SEM passar pelo
    gate de aprovacao do executor. A composite tool externa ja foi aprovada
    pelo usuario (todas as composites destrutivas sao DESTRUCTIVE). As
    sub-tools chamadas aqui ainda passam pelas suas proprias validacoes
    (workspace, command allowlist, schema), mas nao re-prompto. Nao chame
    `_run_tool` com tool destrutiva sem garantir que a composite externa
    seja DESTRUCTIVE — caso contrario o gate fica opaco para o usuario.

    Erros desta funcao seguem o invariante `{ok: false, category, ...}` do
    executor (via `_annotate_error`) — antes retornavam `{\"error\": str}`
    cru, fazendo o modelo aprender N variantes de \"falhou\".
    """
    tool_def = get_tool(name)
    if not tool_def:
        return _annotate_error(
            {"error": f"Tool '{name}' nao encontrada no registry"},
            "unknown_tool",
        )

    # AUDIT_V2.2 #033: the #D110 trust model skips the *approval prompt* for
    # composite-invoked sub-tools (the outer composite was already approved) —
    # but `is_denied` is a HARD BLOCK, not a prompt. A user `deny` rule means
    # "never run", so honour it even inside a composite (e.g. run_tests →
    # execute_shell when the user denied execute_shell).
    from ..approval import is_denied
    denied, reason = is_denied(name, kwargs)
    if denied:
        return _annotate_error(
            {"error": f"Tool '{name}' bloqueada por regra deny: {reason}",
             "denied_by_rule": True},
            "violation",
        )

    # #015: re-enforce the active workspace on sub-tool args. The executor's
    # workspace gate runs only for top-level tool calls; composite-invoked
    # sub-tools bypass it (#D110 trust model). When a workspace is active
    # (sub-agent), validate/rewrite path args here so a composite that builds
    # a path internally can't escape the workspace. No-op when unset (main).
    from ..agents.workspace import active_workspace as _active_ws
    _ws = _active_ws.get()
    if _ws:
        from ..agents import validate_workspace_args
        ok, new_kwargs, ws_err = validate_workspace_args(_ws, name, kwargs)
        if not ok:
            return _annotate_error(
                {"error": ws_err, "workspace_violation": True}, "violation"
            )
        kwargs = new_kwargs

    if timeout is None:
        timeout = _SLOW_TOOL_TIMEOUT if name in _SLOW_TOOLS else TOOL_EXECUTION_TIMEOUT

    # CODE_REVIEW2 #5: mirror the executor's turn-budget clamp so sub-tools
    # invoked by composite tools (project_overview, run_tests,
    # search_and_replace, deploy_check) also respect the wall-clock
    # budget. Without this, a composite started near end of budget would
    # have its outer call cancelled correctly, but inner sub-tools would
    # still log "300s timeout" while the real cap was the clamp.
    from .._turn_budget import clamp as _clamp_budget, remaining as _remaining_budget
    _budget_left = _remaining_budget()
    if _budget_left <= 0:
        return _annotate_error(
            {
                "error": f"Tool '{name}' skipped — turn wall-clock budget exhausted",
                "timeout": True,
            },
            "timeout",
        )
    effective_timeout = _clamp_budget(float(timeout))

    try:
        return await asyncio.wait_for(tool_def.executor(**kwargs), timeout=effective_timeout)
    except (TimeoutError, asyncio.TimeoutError):
        clamped = effective_timeout < timeout
        suffix = f" (clamped from {int(timeout)}s by turn budget)" if clamped else ""
        return _annotate_error(
            {
                "error": (
                    f"Tool '{name}' excedeu timeout de "
                    f"{int(effective_timeout)}s{suffix}"
                ),
                "timeout": True,
            },
            "timeout",
        )
    except Exception as e:
        return _annotate_error(
            {"error": f"Erro ao executar {name}: {e}"},
            "runtime",
        )
