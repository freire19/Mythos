"""
Planning UI: present_plan card + todo_write checklist.

Extracted from `core.py` (Plano-Upgrade-v3 §1.1).
"""

from __future__ import annotations

from ..theme import (
    ELBOW,
    RESULT_CONT,
    RESULT_INDENT,
    DISPLAY_LINE_TRUNCATE,
    DISPLAY_PROMPT_VALUE_TRUNCATE,
    C,
    _truncate,
    c,
    card_bottom,
    card_gutter,
    card_top,
)

_TODO_STATUS_GLYPH = {
    "pending": ("☐", C.GRAY),
    "in_progress": ("☐", C.VIOLET_GLOW),
    "completed": ("☒", C.GREEN_DARK),
    "cancelled": ("☒", C.GRAY_DARK),
}


def _print_todo_list(todos: list) -> None:
    """Checklist hanging off the `● Update Todos` header: the first item
    takes the `⎿` elbow, the rest align under it. Finished items are dimmed
    and struck through; the one in progress is bold."""
    if not todos:
        print(f"{RESULT_INDENT}{c(C.GRAY_DARK, ELBOW)}  {c(C.GRAY, '(empty todo list)')}")
        return
    first = True
    for t in todos:
        if not isinstance(t, dict):
            continue
        status = str(t.get("status", "pending"))
        glyph, color = _TODO_STATUS_GLYPH.get(status, ("•", C.GRAY))
        content = _truncate(str(t.get("content", "")), DISPLAY_LINE_TRUNCATE)
        if status in ("completed", "cancelled"):
            text = c(C.GRAY + C.STRIKE, content)
        elif status == "in_progress":
            text = c(C.BOLD, content)
        else:
            text = content
        lead = f"{RESULT_INDENT}{c(C.GRAY_DARK, ELBOW)}  " if first else RESULT_CONT
        first = False
        print(f"{lead}{c(color, glyph)} {text}")


def _print_plan_card(args: dict) -> None:
    """Pretty-print a present_plan approval card."""
    summary = str(args.get("summary", ""))
    steps = args.get("steps", []) or []
    gut = card_gutter(C.YELLOW)
    print()
    print(card_top("PLANO PROPOSTO", C.YELLOW, width=39))
    print(f"{gut} {c(C.WHITE + C.BOLD, summary)}")
    print(gut)
    for i, step in enumerate(steps, start=1):
        text = _truncate(str(step), DISPLAY_PROMPT_VALUE_TRUNCATE)
        print(f"{gut} {c(C.GRAY, f'{i:>2}.')} {text}")
    print(card_bottom(C.YELLOW, width=39))


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

    gut = card_gutter(C.YELLOW)
    print()
    print(card_top("PRE-FLIGHT", C.YELLOW, width=54))
    print(f"{gut} {c(C.WHITE + C.BOLD, 'Goal:')} {goal}")
    print(gut)
    print(f"{gut} {c(C.WHITE + C.BOLD, 'Planned')} {c(C.GRAY, f'({len(steps)} tool(s)):')}")
    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool", ""))
        preview = _truncate(str(step.get("args_preview", "")), 40)
        cost = float(step.get("cost_usd", 0.0) or 0.0)
        time_s = float(step.get("time_s", 0.0) or 0.0)
        cost_str = f"${cost:.4f}" if cost else "  ~$?"
        line = f"{i:>2}. {tool:<22} {c(C.GRAY, preview):<40}"
        meta = c(C.GRAY, f"{cost_str:>7} {time_s:>5.1f}s")
        print(f"{gut}   {line}  {meta}")

    if alts:
        print(gut)
        print(f"{gut} {c(C.GRAY, 'Rejected:')}")
        for alt in alts:
            if not isinstance(alt, dict):
                continue
            approach = str(alt.get("approach", ""))
            why = str(alt.get("why_rejected", ""))
            line = f"✗ {approach}" + (f" — {why}" if why else "")
            print(f"{gut}   {c(C.GRAY, line)}")

    conf_color = _CONFIDENCE_COLOR.get(confidence, C.YELLOW)
    print(gut)
    print(
        f"{gut} "
        f"{c(C.WHITE + C.BOLD, 'Total:')} "
        f"~${total_cost:.4f}  ~{total_time:.0f}s   "
        f"{c(C.GRAY, 'confidence:')} {c(conf_color, confidence)}"
    )
    print(card_bottom(C.YELLOW, width=54))
