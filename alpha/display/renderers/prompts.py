"""
Approval prompts + session auto-accept state.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

import json
from pathlib import Path

from ...settings import read_json
from ..theme import DISPLAY_PROMPT_VALUE_TRUNCATE, C, _truncate, c
from .planning import _print_plan_card

# Session-level approval state.
#
# Two write paths exist:
#  - `set_auto_accept` / `toggle_auto_accept`: explicit user intent
#    (shift+tab, /accept-edits) — persisted to `~/.alpha/settings.json`
#    so the choice survives REPL restarts.
#  - In-prompt `[a]` (`print_approval_request`): a one-shot "approve
#    rest of session" — stays in-memory only.
#  - `reset_approve_all` (called by /clear): in-memory reset only;
#    leaves the on-disk preference intact.
_AUTO_ACCEPT_SETTING_KEY = "auto_accept_default"


def _auto_accept_settings_path():
    return Path.home() / ".alpha" / "settings.json"


def _load_auto_accept_default() -> bool:
    data = read_json(_auto_accept_settings_path(), default={})
    return bool(data.get(_AUTO_ACCEPT_SETTING_KEY, False)) if isinstance(data, dict) else False


def _persist_auto_accept(value: bool) -> None:
    path = _auto_accept_settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(path, default={})
        if not isinstance(data, dict):
            data = {}
        data[_AUTO_ACCEPT_SETTING_KEY] = bool(value)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Read-only home or quota issue — don't crash the REPL over a preference.
        pass


_approve_all: bool = _load_auto_accept_default()


def reset_approve_all() -> None:
    """Reset the in-memory approve-all flag (called on /clear). Does NOT
    touch the persisted default — that's only changed by explicit
    set_auto_accept/toggle_auto_accept."""
    global _approve_all
    _approve_all = False


def is_auto_accept() -> bool:
    """Whether the session is currently auto-approving destructive tools."""
    return _approve_all


def set_auto_accept(value: bool) -> None:
    """Explicitly turn auto-accept on/off. Used by /accept-edits and shift+tab.
    Persists the choice to `~/.alpha/settings.json`."""
    global _approve_all
    _approve_all = bool(value)
    _persist_auto_accept(_approve_all)


def toggle_auto_accept() -> bool:
    """Flip auto-accept and return the new state. Persists to disk."""
    global _approve_all
    _approve_all = not _approve_all
    _persist_auto_accept(_approve_all)
    return _approve_all


def print_approval_request(tool_name: str, args: dict) -> bool:
    """Show approval request with Kali-style danger indication.

    Returns True if approved. Supports:
    - s/y: approve this action
    - n: deny this action
    - a: approve ALL actions for the rest of this session
    """
    global _approve_all

    # If user previously chose "approve all", auto-approve
    if _approve_all:
        print(f"  {c(C.GREEN, '✦')} {c(C.CYAN, tool_name)} {c(C.GREEN_DARK, '(auto-approved)')}")
        return True

    if tool_name == "present_plan":
        _print_plan_card(args)
    else:
        print()
        print(f"  {c(C.RED + C.BOLD, '┌─ APROVAÇÃO NECESSÁRIA ─────────────────────')}")
        print(f"  {c(C.RED, '│')} Tool: {c(C.CYAN + C.BOLD, tool_name)}")
        if isinstance(args, dict):
            for k, v in args.items():
                val_str = _truncate(str(v), DISPLAY_PROMPT_VALUE_TRUNCATE)
                print(f"  {c(C.RED, '│')} {c(C.GRAY, k)}: {val_str}")
        print(f"  {c(C.RED + C.BOLD, '└────────────────────────────────────────')}")

    try:
        while True:
            resp = input(
                f"\n  {c(C.YELLOW + C.BOLD, 'Aprovar? [s/n/a(ll)]:')} "
            ).strip().lower()
            if resp in ("s", "sim", "y", "yes"):
                print(f"  {c(C.GREEN, '✓ Aprovado')}")
                return True
            if resp in ("n", "não", "nao", "no"):
                print(f"  {c(C.RED, '✗ Negado')}")
                return False
            if resp in ("a", "all", "todos"):
                # Persists across REPL restarts. `/clear` clears it via
                # `reset_approve_all`; an explicit toggle (shift+tab,
                # /accept-edits) can turn it back off.
                set_auto_accept(True)
                print(f"  {c(C.GREEN + C.BOLD, '✓ Aprovado (all — salvo para futuras sessões)')}")
                return True
    except EOFError:
        print(f"  {c(C.GRAY, '(auto-denied — sem terminal interativo)')}")
        return False
    except KeyboardInterrupt:
        # Sem este handler, Ctrl+C durante o prompt mata o REPL inteiro.
        # Tratar como "negado" e devolver controle preserva a sessao.
        print(f"\n  {c(C.RED, '✗ Negado (Ctrl+C)')}")
        return False
