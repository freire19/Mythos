"""Checkpoint restore: /rewind."""

from __future__ import annotations

import time

from alpha import checkpoints
from alpha.display import C, c

from ._types import DispatchResult, ReplContext


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (OSError, OverflowError, ValueError):
        return "?"


def _print_list() -> None:
    items = checkpoints.entries(limit=25)
    if not items:
        print(f"  {c(C.GRAY, 'Nenhum checkpoint nesta sessão/workspace.')}")
        return
    print(f"  {c(C.CYAN + C.BOLD, 'Checkpoints')} {c(C.GRAY, '(mais antigo → mais recente)')}")
    for e in items:
        ts = _fmt_ts(e.get("ts", 0))
        if e.get("kind") == "marker":
            label = e.get("label") or "(turno)"
            print(f"  {c(C.GRAY, f'── {ts} turno:')} {c(C.YELLOW, label)}")
        else:
            tool = e.get("tool") or "?"
            path = e.get("path") or "?"
            tag = "novo" if not e.get("existed") else "edit"
            print(f"     {c(C.GRAY, ts)} {c(C.CYAN, tool)} {path} {c(C.GRAY, f'[{tag}]')}")
    print(
        f"  {c(C.GRAY, 'Use /rewind (último turno com edições) ou /rewind N (últimas N edições).')}"
    )


def _handle_rewind(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """Restore file checkpoints.

    Usage:
        /rewind         # desfaz as edições do último turno
        /rewind 3       # desfaz as últimas 3 edições
        /rewind list    # lista os checkpoints registrados
    """
    if not checkpoints.enabled():
        print(f"  {c(C.YELLOW, 'Checkpoints desativados')} {c(C.GRAY, '(ALPHA_CHECKPOINTS=0)')}")
        return DispatchResult.CONTINUE

    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg in ("list", "ls", "log"):
        _print_list()
        return DispatchResult.CONTINUE

    count: int | None = None
    if arg:
        try:
            count = max(1, int(arg))
        except ValueError:
            print(f"  {c(C.YELLOW, 'Usage:')} /rewind [N|list]")
            return DispatchResult.CONTINUE

    result = checkpoints.rewind(count=count)
    for line in result.get("restored", []):
        print(f"  {c(C.GREEN, '↩')} {line}")
    for item in result.get("skipped", []):
        print(f"  {c(C.YELLOW, '⚠ pulado:')} {item.get('path')} — {item.get('reason')}")
    color = C.GREEN if result.get("restored") else C.GRAY
    print(f"  {c(color, result.get('message', ''))}")
    if result.get("restored"):
        print(
            f"  {c(C.GRAY, 'Atenção: o contexto da conversa não volta — só os arquivos. ')}"
            f"{c(C.GRAY, 'Avise o agente do rewind na próxima mensagem.')}"
        )
    return DispatchResult.CONTINUE
