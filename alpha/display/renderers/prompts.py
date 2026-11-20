"""
Approval prompts + session auto-accept state.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

import textwrap

from ..theme import (
    DISPLAY_PROMPT_RISK_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    C,
    _truncate,
    c,
    fit,
    term_cols,
)
from ..select import MenuCancelled, can_select, select_menu
from .planning import _print_plan_card
from .tools import _display_tool_name

# #066: args whose value IS the action being approved — the user must see the
# whole thing, not a 100-char snippet. Given a generous cap (a multi-KB
# write_file content would otherwise flood the terminal).
_RISK_VALUE_KEYS = frozenset({
    "command", "pipeline", "code", "script", "content", "query", "sql",
})

# Session-level approval state.
#
# SECURITY_TIER0: auto-accept is now a SESSION-ONLY blanket — it is never
# persisted to disk. A single keystroke (shift+tab, in-prompt `[t]`) used to
# write a global "approve every destructive tool forever" flag to
# `~/.alpha/settings.json`; that was a first-order footgun. The durable,
# reviewable way to pre-approve an action is a *scoped* allow-rule in
# `permissions.allow` (see `approval.add_allow_rule`), which the in-prompt
# `[a]` now writes. The blanket survives only until the process exits.
#
#  - `set_auto_accept` / `toggle_auto_accept`: explicit session toggle
#    (shift+tab, /accept-edits) — in-memory only.
#  - In-prompt `[a]`: persist a scoped allow-rule for THIS tool+arg.
#  - In-prompt `[t]`: session blanket (in-memory), same as shift+tab.
#  - `reset_approve_all` (called by /clear): in-memory reset.

_approve_all: bool = False


def reset_approve_all() -> None:
    """Reset the in-memory approve-all flag (called on /clear)."""
    global _approve_all
    _approve_all = False


def is_auto_accept() -> bool:
    """Whether the session is currently auto-approving destructive tools."""
    return _approve_all


def set_auto_accept(value: bool) -> None:
    """Turn the session auto-accept blanket on/off (shift+tab, /accept-edits).

    SECURITY_TIER0: in-memory only — never persisted. The blanket resets when
    the process exits."""
    global _approve_all
    _approve_all = bool(value)


def toggle_auto_accept() -> bool:
    """Flip the session auto-accept blanket and return the new state."""
    global _approve_all
    _approve_all = not _approve_all
    return _approve_all


def _record_preflight_decision(tool_name: str, args: dict, decision: str) -> None:
    """Slice 2: capture pre_flight approve/reject for future estimator
    learning. No-op for any other tool (only pre_flight cards carry the
    structured shape `preflight.feedback` expects)."""
    if tool_name != "pre_flight":
        return
    try:
        from ...preflight import record_decision
        record_decision(args, decision)
    except Exception:
        # Feedback log is best-effort — never fail the approval flow on
        # a disk-write error or import problem.
        pass


# (answer key, label) — the number shown is the 1-based position. The keys
# are the legacy letters, still accepted when typed directly.
_APPROVAL_OPTIONS = (
    ("s", "Sim"),
    ("a", "Sim, e não perguntar de novo para esta ação (salva regra)"),
    ("t", "Sim, para tudo nesta sessão"),
    ("n", "Não"),
)

# Longest value shown in full inside the permission card, in lines.
_CARD_MAX_VALUE_LINES = 24


def _print_permission_card(tool_name: str, args: dict) -> None:
    """Rounded card stating WHAT is about to run: the tool and every
    argument, the risk-bearing ones (command, code, content…) in full."""
    edge = C.AMBER_SOFT
    width = max(30, min(term_cols() - 4, 76))
    gut = f"  {c(edge, '│')}"
    title = " Permissão necessária "
    print()
    print(f"  {c(edge, '╭─' + title + '─' * max(0, width - len(title) - 2))}")
    print(f"{gut} {c(C.BOLD, _display_tool_name(tool_name))} {c(C.GRAY_DARK, tool_name)}")
    if isinstance(args, dict):
        for k, v in args.items():
            risky = k in _RISK_VALUE_KEYS
            cap = DISPLAY_PROMPT_RISK_TRUNCATE if risky else DISPLAY_PROMPT_VALUE_TRUNCATE
            raw = _truncate(str(v), cap)
            if risky:
                # Shown in full: wrap inside the card instead of letting
                # the terminal wrap past the gutter.
                val_lines = []
                for ln in raw.split("\n"):
                    val_lines.extend(
                        textwrap.wrap(
                            ln, width=max(20, term_cols() - 8),
                            replace_whitespace=False, drop_whitespace=False,
                        ) or [""]
                    )
            else:
                val_lines = [fit(" ".join(raw.split()), used=len(k) + 7)]
            hidden = max(0, len(val_lines) - _CARD_MAX_VALUE_LINES)
            val_lines = val_lines[:_CARD_MAX_VALUE_LINES]
            if len(val_lines) == 1:
                print(f"{gut} {c(C.GRAY, k + ':')} {val_lines[0]}")
            else:
                print(f"{gut} {c(C.GRAY, k + ':')}")
                for ln in val_lines:
                    print(f"{gut}   {ln}")
            if hidden:
                print(f"{gut}   {c(C.GRAY_DARK, f'… +{hidden} linhas')}")
    print(f"  {c(edge, '╰' + '─' * width)}")
    print()


def print_approval_request(tool_name: str, args: dict) -> bool:
    """Show the permission card and a numbered menu; return True if approved.

    Answers (number or legacy letter):
    - 1 / s / y: approve this action (once)
    - 2 / a: approve AND persist a scoped allow-rule for it
    - 3 / t: approve every destructive tool for the rest of this session
             (in-memory blanket, not persisted)
    - 4 / n: deny this action
    """
    # AUDIT_V2.2 #023: no `global _approve_all` here — this function only reads
    # it (the assignments live in set/toggle/reset helpers). A `global` on a
    # read-only name is a no-op that falsely signals mutation.

    # If user previously chose "approve all", auto-approve
    if _approve_all:
        print(f"  {c(C.GRAY_DARK, '⏵⏵ ' + tool_name + ' auto-aprovado')}")
        _record_preflight_decision(tool_name, args, "auto_approve")
        return True

    if tool_name == "present_plan":
        _print_plan_card(args)
    elif tool_name == "pre_flight":
        from .planning import _print_preflight_card
        _print_preflight_card(args)
    else:
        _print_permission_card(tool_name, args)

    n_opts = len(_APPROVAL_OPTIONS)
    interactive = can_select()
    if not interactive:
        for num, (_, label) in enumerate(_APPROVAL_OPTIONS, start=1):
            pointer = c(C.VIOLET_GLOW, "❯") if num == 1 else " "
            print(f"  {pointer} {c(C.GRAY, f'{num}.')} {label}")

    # Remote control: the same question is offered on the page; whichever
    # side answers first wins (the remote answer releases the menu through
    # its `cancel` event). Only the arrow-key menu can be released that way.
    from ... import remote as _remote

    remote_q = None
    if interactive and _remote.is_active():
        title, detail = _remote.describe_approval(tool_name, args)
        remote_q = _remote.ask(
            "approval", title, detail, [label for _, label in _APPROVAL_OPTIONS]
        )
    outcome = "✗ Negado"

    try:
        while True:
            if interactive:
                # Arrow keys + Enter, digits, or the legacy letters. Esc = deny.
                try:
                    picked = select_menu(
                        [[label] for _, label in _APPROVAL_OPTIONS],
                        shortcuts={"s": 0, "y": 0, "a": 1, "t": 2, "n": 3},
                        hint="↑/↓ para mover · Enter confirma · 1-4 ou s/a/t/n · Esc nega",
                        cancel=remote_q.event if remote_q is not None else None,
                    )
                except MenuCancelled:
                    picked = remote_q.answer if remote_q is not None else None
                    print(f"  {c(C.GRAY_DARK, '(respondido pelo controle remoto)')}")
                resp = "n" if picked is None else _APPROVAL_OPTIONS[picked][0]
            else:
                resp = input(
                    f"\n  {c(C.GRAY, f'Escolha [1-{n_opts}] (ou s/a/t/n):')} "
                ).strip().lower()
            if resp.isdigit() and 1 <= int(resp) <= n_opts:
                resp = _APPROVAL_OPTIONS[int(resp) - 1][0]
            if resp in ("s", "sim", "y", "yes"):
                print(f"  {c(C.GREEN, '✓ Aprovado')}")
                _record_preflight_decision(tool_name, args, "approve")
                outcome = "✓ Aprovado"
                return True
            if resp in ("n", "não", "nao", "no"):
                print(f"  {c(C.RED, '✗ Negado')}")
                _record_preflight_decision(tool_name, args, "reject")
                return False
            if resp in ("a", "always", "sempre"):
                # SECURITY_TIER0: persist a SCOPED allow-rule for this exact
                # tool+arg (e.g. `execute_shell(npm test)`), not a blanket.
                from ...approval import add_allow_rule
                rule = add_allow_rule(tool_name, args)
                if rule:
                    print(f"  {c(C.GREEN, '✓ Aprovado — regra salva:')} {c(C.CYAN, rule)}")
                else:
                    print(f"  {c(C.GREEN, '✓ Aprovado')} {c(C.GRAY, '(não foi possível salvar a regra)')}")
                _record_preflight_decision(tool_name, args, "approve_scoped")
                outcome = "✓ Aprovado (regra salva)"
                return True
            if resp in ("t", "todos", "toda", "all"):
                # In-memory blanket for the rest of this session only.
                set_auto_accept(True)
                print(f"  {c(C.GREEN, '✓ Aprovado (sessão inteira — não persistido)')}")
                _record_preflight_decision(tool_name, args, "approve_all")
                outcome = "✓ Aprovado (sessão inteira)"
                return True
    except EOFError:
        print(f"  {c(C.GRAY, '(auto-denied — sem terminal interativo)')}")
        _record_preflight_decision(tool_name, args, "eof")
        return False
    except KeyboardInterrupt:
        # Sem este handler, Ctrl+C durante o prompt mata o REPL inteiro.
        # Tratar como "negado" e devolver controle preserva a sessao.
        print(f"\n  {c(C.RED, '✗ Negado (Ctrl+C)')}")
        _record_preflight_decision(tool_name, args, "interrupt")
        return False
    finally:
        _remote.close_question(remote_q, outcome)
