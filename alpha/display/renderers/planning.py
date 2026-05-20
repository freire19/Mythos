"""
Planning UI: present_plan card + todo_write checklist.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

from ..theme import DISPLAY_PROMPT_VALUE_TRUNCATE, C, c

_TODO_STATUS_GLYPH = {
    "pending": ("☐", C.GRAY),
    "in_progress": ("◐", C.YELLOW),
    "completed": ("☑", C.GREEN),
    "cancelled": ("☒", C.RED_DARK),
}


def _print_todo_list(todos: list) -> None:
    if not todos:
        print(f"  {c(C.GRAY, '(empty todo list)')}")
        return
    for t in todos:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status", "pending"))
        glyph, color = _TODO_STATUS_GLYPH.get(status, ("•", C.GRAY))
        content = str(t.get("content", ""))
        if len(content) > 200:
            content = content[:197] + "..."
        line_color = C.GRAY if status in ("completed", "cancelled") else C.WHITE
        print(f"  {c(color, glyph)} {c(line_color, content)}")


def _print_plan_card(args: dict) -> None:
    """Pretty-print a present_plan approval card."""
    summary = str(args.get("summary", ""))
    steps = args.get("steps", []) or []
    print()
    print(f"  {c(C.YELLOW + C.BOLD, '┌─ PLANO PROPOSTO ─────────────────────')}")
    print(f"  {c(C.YELLOW, '│')} {c(C.WHITE + C.BOLD, summary)}")
    print(f"  {c(C.YELLOW, '│')}")
    for i, step in enumerate(steps, start=1):
        text = str(step)
        if len(text) > DISPLAY_PROMPT_VALUE_TRUNCATE:
            text = text[:DISPLAY_PROMPT_VALUE_TRUNCATE - 3] + "..."
        print(f"  {c(C.YELLOW, '│')} {c(C.GRAY, f'{i:>2}.')} {text}")
    print(f"  {c(C.YELLOW + C.BOLD, '└──────────────────────────────────────')}")


_CONFIDENCE_COLOR = {
    "high": C.GREEN,
    "medium": C.YELLOW,
    "low": C.RED_DARK,
}


def _print_preflight_card(args: dict) -> None:
    """Pretty-print a pre_flight strategy approval card.

    Distinct from _print_plan_card because pre_flight carries quantified
    cost/time and structured per-step rows. Same visual family (yellow
    box) so the user reads them as variants of the same "review before
    execute" gesture. Per-step `cost_usd`/`time_s` are precomputed by
    the `_pre_flight` executor and attached to each step dict — the
    renderer just reads (no re-estimation).
    """
    goal = str(args.get("goal", ""))
    steps = args.get("steps", []) or []
    alts = args.get("alternatives_rejected", []) or []
    confidence = str(args.get("confidence", "medium"))
    total_cost = float(args.get("estimated_cost_usd", 0.0) or 0.0)
    total_time = float(args.get("estimated_time_s", 0.0) or 0.0)

    print()
    print(f"  {c(C.YELLOW + C.BOLD, '┌─ PRE-FLIGHT ────────────────────────────────────────')}")
    print(f"  {c(C.YELLOW, '│')} {c(C.WHITE + C.BOLD, 'Goal:')} {goal}")
    print(f"  {c(C.YELLOW, '│')}")
    print(f"  {c(C.YELLOW, '│')} {c(C.WHITE + C.BOLD, 'Planned')} {c(C.GRAY, f'({len(steps)} tool(s)):')}")
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool", ""))
        preview = str(step.get("args_preview", ""))
        if len(preview) > 40:
            preview = preview[:37] + "..."
        cost = float(step.get("cost_usd", 0.0) or 0.0)
        time_s = float(step.get("time_s", 0.0) or 0.0)
        cost_str = f"${cost:.4f}" if cost else "  ~$?"
        line = f"{i:>2}. {tool:<22} {c(C.GRAY, preview):<40}"
        meta = c(C.GRAY, f"{cost_str:>7} {time_s:>5.1f}s")
        print(f"  {c(C.YELLOW, '│')}   {line}  {meta}")

    if alts:
        print(f"  {c(C.YELLOW, '│')}")
        print(f"  {c(C.YELLOW, '│')} {c(C.GRAY, 'Rejected:')}")
        for alt in alts:
            if not isinstance(alt, dict):
                continue
            approach = str(alt.get("approach", ""))
            why = str(alt.get("why_rejected", ""))
            line = f"✗ {approach}" + (f" — {why}" if why else "")
            print(f"  {c(C.YELLOW, '│')}   {c(C.GRAY, line)}")

    conf_color = _CONFIDENCE_COLOR.get(confidence, C.YELLOW)
    print(f"  {c(C.YELLOW, '│')}")
    print(
        f"  {c(C.YELLOW, '│')} "
        f"{c(C.WHITE + C.BOLD, 'Total:')} "
        f"~${total_cost:.4f}  ~{total_time:.0f}s   "
        f"{c(C.GRAY, 'confidence:')} {c(conf_color, confidence)}"
    )
    print(f"  {c(C.YELLOW + C.BOLD, '└─────────────────────────────────────────────────────')}")
