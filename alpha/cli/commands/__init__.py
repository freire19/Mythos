"""Slash-command dispatch for the REPL.

#DM041 split: was a single 1091-line `commands.py`. Now a sub-package
where each handler lives in a focused module by domain:

- ``_types``       — `DispatchResult`, `ReplContext` (shared state)
- ``session``      — /exit /clear /save /load /continue /sessions /history
- ``agent_meta``   — /agent /agents /model /tools /skills /mcp /init
- ``cost``         — /cost /stats /preflight /context
- ``compact_cmd``  — /compact (manual context compression, optional focus)
- ``export_cmd``   — /export (conversation → Markdown file)
- ``doctor_cmd``   — /doctor (environment health check, no network)
- ``safety``       — /accept-edits /sandbox
- ``permissions_cmd`` — /permissions (allow/deny rules of settings.json)
- ``memory_cmd``   — /memory
- ``io``           — /image /pdf /audio (need raw `user_input`, not just `parts`)
- ``review_cmd``   — /review (diff collected locally, falls through to the LLM)
- ``help``         — /help (kept separate so the help list stays close to
                     the catalogue without pulling the whole table back in)

This module exposes only the public API consumed by `main.py`:
``DispatchResult``, ``ReplContext``, ``dispatch``. The ``_DISPATCH`` table
plus ``_try_skill_dispatch`` glue handlers to slash names.

## Contract

Every handler takes ``(ctx, parts)`` and returns a ``DispatchResult``:

- ``CONTINUE``: command handled, REPL loop should ``continue``.
- ``BREAK``: command handled, REPL loop should ``break`` (e.g. ``/exit``).
- ``FALL_THROUGH``: command transformed the input; the REPL should
  proceed with ``ctx.user_input_override`` (and optionally
  ``ctx.image_paths_override``) as if the user had typed it normally.
  Used by ``/init``, ``/<skill>``, ``/image``, ``/pdf``, ``/audio``.

``ReplContext`` carries the mutable state the handlers need to read or
update (``messages``, ``history``, ``session_id``, ``provider``, etc.).
Handlers mutate the context in place — there's no functional purity to
preserve here, the original behavior was full of ``messages[:] = [...]``
in-place rebinds.

The integration tests in ``tests/integration/test_repl_flow.py`` lock
in the user-visible behavior so this refactor is safe.
"""

from __future__ import annotations

import shutil
from difflib import get_close_matches
from typing import Callable

from alpha.display import C, c
from alpha.skills import get_skill, list_skills

from ._types import DispatchResult, ReplContext
from .agent_meta import (
    _handle_agent,
    _handle_agents,
    _handle_init,
    _handle_mcp,
    _handle_model,
    _handle_skills,
    _handle_tools,
)
from .compact_cmd import _handle_compact
from .cost import _handle_context, _handle_cost, _handle_preflight, _handle_stats
from .doctor_cmd import _handle_doctor
from .export_cmd import _handle_export
from .help import _handle_help
from .io import handle_audio, handle_image, handle_pdf
from .bg import _handle_tasks
from .memory_cmd import _handle_memory
from .permissions_cmd import _handle_permissions
from .schedule_cmd import _handle_loop
from .review_cmd import _handle_review
from .rewind import _handle_rewind
from .safety import _handle_accept_edits, _handle_plan, _handle_sandbox
from .session import (
    _handle_clear,
    _handle_continue,
    _handle_exit,
    _handle_history,
    _handle_load,
    _handle_save,
    _handle_sessions,
)

__all__ = ["DispatchResult", "ReplContext", "dispatch", "iter_commands"]


# #DM068: single source of truth for the command catalogue. Descriptions
# live next to `_DISPATCH` so adding a command means editing ONE file;
# `/help` (help.py) and the REPL autocomplete (repl_input.py) both derive
# from `iter_commands()` instead of keeping their own drifting lists.
# Insertion order is the display order.
_DESCRIPTIONS: dict[str, str] = {
    "/init": "Draft an ALPHA.md for this project",
    "/clear": "Clear history and screen",
    "/history": "Show conversation history",
    "/save": "Save current session",
    "/load": "Load a previous session",
    "/continue": "Resume from last session",
    "/sessions": "List saved sessions",
    "/export": "Write the conversation to a Markdown file (/export [path] [--force])",
    "/doctor": "Check the environment: provider keys, gh/git, browser, sandbox, MCP, memory",
    "/context": "Show context window usage",
    "/compact": "Summarize the conversation now to free context (/compact <what to keep>)",
    "/cost": "Show token usage and estimated USD cost for this session",
    "/stats": "Show iteration/tool/approval stats for this session",
    "/preflight": "Show pre_flight decision analytics (approve/reject log)",
    "/memory": "Manage cross-session memory (list|forget <i>|clear|edit [workspace|global])",
    "/accept-edits": "Toggle auto-approve for destructive tools (shift+tab)",
    "/permissions": "List/edit allow-deny rules in settings.json (allow|deny <rule>, remove <rule|#n>)",
    "/plan": "Toggle plan mode: read-only até aprovar um plano (shift+tab)",
    "/tools": "List available tools",
    "/skills": "List registered skills (ready vs inactive)",
    "/mcp": "List connected MCP servers",
    "/image": "Attach an image (Ctrl+V or Alt+V also works)",
    "/pdf": "Attach a PDF (text extracted via pypdf)",
    "/audio": "Attach audio (transcribed via OpenAI Whisper)",
    "/agents": "List named agents",
    "/agent": "Show/switch active agent",
    "/model": "Show/switch provider & model",
    "/sandbox": "Show sandbox state for destructive shell tools",
    "/rewind": "Undo file edits from checkpoints (last turn | N edits | list)",
    "/tasks": "List background tasks (execute_shell background=true)",
    "/loop": "Schedule a prompt/command on an interval (/loop 5m /tasks | once | list | stop)",
    "/review": "Code review of the working tree, a PR (gh), a branch, a commit or a path (--deep, --fix, --comment)",
    "/help": "Show all commands",
    "/exit": "Exit",
}

# Alias → canonical. Dispatchable but hidden from /help and autocomplete
# (matches the pre-#DM068 behavior of both surfaces).
_ALIASES: dict[str, str] = {
    "/quit": "/exit",
    "/q": "/exit",
    "/accept_edits": "/accept-edits",
}


def iter_commands() -> list[tuple[str, str]]:
    """Canonical ``(command, description)`` pairs, in display order.

    The single source consumed by ``/help`` and the slash autocomplete
    (#DM068). Aliases are excluded.
    """
    return list(_DESCRIPTIONS.items())


_DISPATCH: dict[str, Callable[[ReplContext, list[str]], DispatchResult]] = {
    "/exit": _handle_exit,
    "/quit": _handle_exit,
    "/q": _handle_exit,
    "/clear": _handle_clear,
    "/history": _handle_history,
    "/save": _handle_save,
    "/load": _handle_load,
    "/continue": _handle_continue,
    "/sessions": _handle_sessions,
    "/export": _handle_export,
    "/doctor": _handle_doctor,
    "/tools": _handle_tools,
    "/skills": _handle_skills,
    "/mcp": _handle_mcp,
    "/agents": _handle_agents,
    "/agent": _handle_agent,
    "/model": _handle_model,
    "/init": _handle_init,
    "/context": _handle_context,
    "/compact": _handle_compact,
    "/accept-edits": _handle_accept_edits,
    "/permissions": _handle_permissions,
    "/accept_edits": _handle_accept_edits,
    "/plan": _handle_plan,
    "/help": _handle_help,
    "/cost": _handle_cost,
    "/stats": _handle_stats,
    "/preflight": _handle_preflight,
    "/memory": _handle_memory,
    "/sandbox": _handle_sandbox,
    "/rewind": _handle_rewind,
    "/tasks": _handle_tasks,
    "/loop": _handle_loop,
    "/review": _handle_review,
}


def _try_skill_dispatch(
    ctx: ReplContext, cmd: str, parts: list[str], user_input: str
) -> DispatchResult:
    skill_name = cmd[1:]
    skill = get_skill(skill_name)

    if skill is None:
        suggestion = get_close_matches(
            skill_name, [s.name for s in list_skills()], n=1
        )
        hint = f" Did you mean /{suggestion[0]}?" if suggestion else ""
        print(c(C.GRAY, f"  Unknown command: {cmd}.{hint}"))
        return DispatchResult.CONTINUE

    skill_args = user_input.split(maxsplit=1)[1] if len(parts) > 1 else ""
    missing = [b for b in skill.requires_bins if not shutil.which(b)]
    if missing:
        print(
            f"  {c(C.YELLOW, '⚠')} Skill '{skill.name}' requires "
            f"bins not on PATH: {', '.join(missing)}"
        )

    # Pin this as the active skill so the model can't autonomously chain into
    # OTHER skills mid-task (the load_skill tool hard-blocks while one is
    # active). Cleared on the next fresh interactive prompt by run_repl.
    from alpha.tools.skill_tools import set_active_skill
    set_active_skill(skill.name)

    ctx.user_input_override = (
        f"[Skill invoked via /{skill.name}]\n"
        "--- BEGIN SKILL INSTRUCTIONS ---\n"
        f"{skill.body}\n"
        "--- END SKILL INSTRUCTIONS ---\n\n"
        f"User input: {skill_args or '(no additional args)'}\n"
        "Follow the skill's instructions above to handle this."
    )
    print(
        f"  {c(C.GREEN, '✦')} Loaded skill: "
        f"{c(C.CYAN, skill.name)} "
        f"{c(C.GRAY, f'({len(skill.body)} chars)')}"
    )
    return DispatchResult.FALL_THROUGH


def dispatch(ctx: ReplContext, user_input: str) -> DispatchResult:
    """Entry point called by the REPL loop on slash-command input.

    The caller is expected to have verified that the line starts with
    ``/`` and that the first token has no embedded slash (paths like
    ``/home/user/file`` should NOT reach this function — they're normal
    input).
    """
    parts = user_input.split()
    cmd = parts[0].lower()

    if cmd == "/image":
        return handle_image(ctx, user_input, parts)
    if cmd == "/pdf":
        return handle_pdf(ctx, user_input, parts)
    if cmd == "/audio":
        return handle_audio(ctx, user_input, parts)

    handler = _DISPATCH.get(cmd)
    if handler is not None:
        return handler(ctx, parts)

    return _try_skill_dispatch(ctx, cmd, parts, user_input)
