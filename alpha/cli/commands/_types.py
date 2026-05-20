"""Shared types for the slash-command dispatch system (#DM041).

Lives in its own module so each handler sub-module (session.py,
agent_meta.py, etc.) can import without pulling in the dispatch table
or the other handlers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from alpha.agents import AgentScope


class DispatchResult(enum.Enum):
    """What the REPL loop should do after a handler runs."""

    CONTINUE = "continue"
    BREAK = "break"
    FALL_THROUGH = "fall_through"


@dataclass
class ReplContext:
    """Mutable state shared between the REPL loop and command handlers.

    Handlers mutate the relevant fields in place. Two ``*_override``
    fields exist to carry transformed input from FALL_THROUGH commands
    back to the REPL loop.
    """

    # Conversation state
    messages: list[dict]
    history: list[dict]
    session_id: str

    # Provider/agent state
    provider: str
    temperature: float
    cfg: dict[str, Any]
    system_prompt: str
    tools: list[dict]
    get_tool_fn: Callable | None
    active_agent: AgentScope | None

    # FALL_THROUGH outputs — set by /init, /<skill>, /image when they
    # transform the user's input before the LLM call.
    user_input_override: str | None = None
    image_paths_override: list[Path] | None = None
    skip_history_record: bool = field(default=False)
    history_record_override: str | None = None
