"""Tools de gestão de background tasks (ver alpha/bg_tasks.py).

`execute_shell(background=true)` cria a task; estas tools dão ao modelo
visibilidade (list/output) e controle (kill). Auto-descobertas pelo
`load_all_tools()` (sufixo _tools.py).
"""

from __future__ import annotations

from .. import bg_tasks
from . import ToolCategory, ToolDefinition, ToolSafety, register_tool


async def _bg_tasks_list() -> dict:
    tasks = bg_tasks.list_tasks()
    if not tasks:
        return {"tasks": [], "message": "Nenhuma background task nesta sessão"}
    return {"tasks": tasks, "count": len(tasks)}


async def _bg_task_output(task_id: int, tail_chars: int = 4000) -> dict:
    return bg_tasks.output(task_id, tail_chars=tail_chars)


async def _bg_task_kill(task_id: int) -> dict:
    return bg_tasks.kill(task_id)


register_tool(
    ToolDefinition(
        name="bg_tasks_list",
        description=(
            "List background tasks started with execute_shell(background=true): "
            "status (running/finished), exit code, runtime, log path."
        ),
        parameters={"type": "object", "properties": {}},
        safety=ToolSafety.SAFE,
        category=ToolCategory.SHELL,
        executor=_bg_tasks_list,
    )
)

register_tool(
    ToolDefinition(
        name="bg_task_output",
        description=(
            "Read the output (log tail) of a background task. Works while "
            "it runs and after it finishes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id from execute_shell(background=true)"},
                "tail_chars": {
                    "type": "integer",
                    "description": "How many bytes from the end of the log (default 4000, max 64000)",
                    "default": 4000,
                },
            },
            "required": ["task_id"],
        },
        safety=ToolSafety.SAFE,
        category=ToolCategory.SHELL,
        executor=_bg_task_output,
    )
)

register_tool(
    ToolDefinition(
        name="bg_task_kill",
        description="Terminate a running background task (SIGTERM, then SIGKILL after 3s).",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id to terminate"},
            },
            "required": ["task_id"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        category=ToolCategory.SHELL,
        executor=_bg_task_kill,
    )
)
