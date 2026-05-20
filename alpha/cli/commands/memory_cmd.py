"""Memory handler: /memory list|forget|clear|edit [workspace|global]."""

from __future__ import annotations

import os

from alpha.display import C, c
from alpha.memory import (
    clear as memory_clear,
    forget as memory_forget,
    list_entries as memory_list_entries,
    memory_path,
)

from ._types import DispatchResult, ReplContext


def _handle_memory(ctx: ReplContext, parts: list[str]) -> DispatchResult:
    """`/memory list|forget|clear|edit [scope]` — manage cross-session memory.

    Scope defaults to "workspace". Pass "global" for the second arg to
    target the user-global memory."""
    sub = parts[1] if len(parts) > 1 else "list"
    scope = "workspace"
    extra = parts[2:] if len(parts) > 2 else []
    # Look for a scope keyword anywhere in extra args; everything else
    # is sub-command-specific (e.g. an index for `forget`).
    for arg in list(extra):
        if arg in ("workspace", "global"):
            scope = arg
            extra.remove(arg)

    if sub == "list":
        entries = memory_list_entries(scope=scope)
        if not entries:
            print(f"  {c(C.GRAY, f'(no entries in {scope} memory)')}")
            return DispatchResult.CONTINUE
        print(f"  {c(C.VIOLET + C.BOLD, f'Memory ({scope}) — newest first:')}")
        for i, e in enumerate(entries, start=1):
            head = f"#{i} {e['ts']} ({e['kind']})"
            print(f"  {c(C.CYAN, head)}")
            for line in e["body"].splitlines():
                print(f"    {c(C.GRAY, line)}")
        return DispatchResult.CONTINUE

    if sub == "forget":
        if not extra:
            print(f"  {c(C.YELLOW, 'Usage:')} /memory forget <index> [workspace|global]")
            return DispatchResult.CONTINUE
        try:
            idx = int(extra[0])
        except ValueError:
            print(f"  {c(C.RED, f'invalid index: {extra[0]!r}')}")
            return DispatchResult.CONTINUE
        out = memory_forget(idx, scope=scope)
        if out.get("ok"):
            r = out["removed"]
            print(f"  {c(C.GREEN, '✓')} forgot {c(C.CYAN, r['ts'])} ({r['kind']}): {r['body'][:80]}")
        else:
            print(f"  {c(C.RED, '✗')} {out.get('error', 'failed')}")
        return DispatchResult.CONTINUE

    if sub == "clear":
        out = memory_clear(scope=scope)
        print(f"  {c(C.GREEN, '✓')} cleared {out['removed_count']} entry/entries from {scope} memory")
        return DispatchResult.CONTINUE

    if sub == "edit":
        p = memory_path(scope=scope)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        rc = os.system(f"{editor} {p!s}")
        print(f"  {c(C.GRAY, f'editor exit code: {rc} — memory file: {p}')}")
        return DispatchResult.CONTINUE

    print(f"  {c(C.YELLOW, 'Usage:')} /memory <list|forget <i>|clear|edit> [workspace|global]")
    return DispatchResult.CONTINUE
