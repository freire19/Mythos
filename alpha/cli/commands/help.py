"""/help — list available slash commands.

#DM068: the list is derived from the single-source catalogue in
``alpha.cli.commands`` (``iter_commands``) instead of a hand-kept copy.
The import is deferred to call time to avoid a circular import (this
module is imported by the package ``__init__``).
"""

from __future__ import annotations

from alpha.display import C, c

from ._types import DispatchResult, ReplContext


def _print_entry(name: str, desc: str) -> None:
    # Pad plain text (not the colored string) so columns align; commands
    # longer than 9 chars just get the single separator space.
    pad = " " * max(0, 9 - len(name))
    print(f"  {c(C.CYAN, name)}{pad} — {desc}")


def _handle_help(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    from alpha.cli.commands import iter_commands

    for name, desc in iter_commands():
        if name == "/exit":
            # Keep the /<skill> pseudo-entry right before /exit, as before.
            _print_entry("/<skill>", "Invoke a skill by name (e.g. /skill-creator)")
            _print_entry("!<cmd>", "Run a shell command yourself; its output rides with your next message")
        _print_entry(name, desc)
    return DispatchResult.CONTINUE
