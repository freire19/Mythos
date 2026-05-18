"""
Memory tool — lets the agent persist notes across sessions.

Plano-Upgrade-v3 §3.1 / H2 #10. The agent calls `record_memory(...)`
when it learns something worth keeping (a user preference, a project
convention, a fix recipe). Reading is via `summary_for_prompt` in
`alpha/memory.py`, which the system-prompt loader can inject.

Slash commands (`/memory list|forget|clear`) live in
`alpha/cli/commands.py`; this module only exposes the write path the
agent uses autonomously.
"""

from __future__ import annotations

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..memory import record


async def _record_memory(
    content: str,
    kind: str = "note",
    scope: str = "workspace",
) -> dict:
    """Persist a memory entry. Auto-trims to fit the 8KB cap."""
    return record(content, scope=scope, kind=kind)


register_tool(
    ToolDefinition(
        name="record_memory",
        description=(
            "Persist a short note across sessions. Use when you learn "
            "something the next session would benefit from knowing: user "
            "preferences ('responds best in Portuguese'), recurring "
            "project patterns ('this repo pins deps with upper bounds'), "
            "or fix recipes ('AsyncClient must be loop-local here'). "
            "DO NOT use for ephemeral state (in-progress task tracking — "
            "use todo_write) or session-only context (just keep it in "
            "the conversation). Memory is read into future sessions' "
            "system prompts; keep entries terse and lasting."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The note to remember. Write as a complete "
                        "sentence; future sessions see it without "
                        "surrounding context."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Classification tag: 'preference' (user style/voice), "
                        "'pattern' (project convention), 'fix' (gotcha + "
                        "resolution), 'note' (default — anything else)."
                    ),
                    "default": "note",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "'workspace' (default — scoped to current project "
                        "directory) or 'global' (applies everywhere)."
                    ),
                    "enum": ["workspace", "global"],
                    "default": "workspace",
                },
            },
            "required": ["content"],
        },
        safety=ToolSafety.SAFE,
        executor=_record_memory,
        category=ToolCategory.SYSTEM,
    )
)
