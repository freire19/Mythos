"""Background tasks: /tasks."""

from __future__ import annotations

from alpha import bg_tasks
from alpha.display import C, c

from ._types import DispatchResult, ReplContext


def _handle_tasks(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """List background tasks (execute_shell background=true)."""
    tasks = bg_tasks.list_tasks()
    if not tasks:
        print(f"  {c(C.GRAY, 'Nenhuma background task nesta sessão.')}")
        return DispatchResult.CONTINUE
    print(f"  {c(C.CYAN + C.BOLD, 'Background tasks')}")
    for t in tasks:
        if t["status"] == "running":
            status = c(C.GREEN, "● running")
        elif t.get("exit_code") == 0:
            status = c(C.GRAY, "✓ ok")
        else:
            status = c(C.RED, f"✗ exit {t.get('exit_code')}")
        runtime = c(C.GRAY, f"{t['runtime_s']}s")
        print(f"  #{t['task_id']} {status} {runtime} {t['command'][:70]}")
        print(f"     {c(C.GRAY_DARK, 'log: ' + t['log'])}")
    return DispatchResult.CONTINUE
