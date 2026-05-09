"""Shared helpers for composite tool modules.

Extraido de composite_tools.py durante o split em 4 modulos (#030).
"""

import asyncio
import logging

from ..executor import (
    TOOL_EXECUTION_TIMEOUT,
    _SLOW_TOOL_TIMEOUT,
    _SLOW_TOOLS,
    _annotate_error,
)
from . import get_tool

logger = logging.getLogger(__name__)


def _violation(msg: str) -> dict:
    """Workspace-violation result with the executor's standard error invariant."""
    return _annotate_error({"error": msg, "workspace_violation": True}, "violation")


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
    (workspace, command allowlist, schema), mas nao re-prompto.
    """
    tool_def = get_tool(name)
    if not tool_def:
        return _annotate_error(
            {"error": f"Tool '{name}' nao encontrada no registry"},
            "unknown_tool",
        )

    if timeout is None:
        timeout = _SLOW_TOOL_TIMEOUT if name in _SLOW_TOOLS else TOOL_EXECUTION_TIMEOUT

    try:
        return await asyncio.wait_for(tool_def.executor(**kwargs), timeout=timeout)
    except TimeoutError:
        return _annotate_error(
            {
                "error": f"Tool '{name}' excedeu timeout de {timeout}s",
                "timeout": True,
            },
            "timeout",
        )
    except Exception as e:
        return _annotate_error(
            {"error": f"Erro ao executar {name}: {e}"},
            "runtime",
        )
