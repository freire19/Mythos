"""Interaction tools — ways for the agent to ask the user for input.

Currently exposes `ask_choice` for discrete multiple-choice questions.
The tool blocks the agent loop while waiting for the user to type a
number (or the option text), pauses the thinking spinner around the
prompt so the menu stays clean, and returns the picked value to the
LLM. Skills that previously rendered a markdown table and asked the
user to type a category should call this tool instead.
"""

import asyncio
import json
import logging

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool
from ..display.core import C, c

logger = logging.getLogger(__name__)


def _parse_options(raw: object) -> list[str]:
    """Accept options as list[str] or a JSON-encoded string. LLMs sometimes
    pass arrays as JSON-string when the schema is `array` — tolerate both."""
    if isinstance(raw, list):
        return [str(o) for o in raw if str(o).strip()]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(o) for o in parsed if str(o).strip()]
        except json.JSONDecodeError:
            pass
    return []


def _render_menu(question: str, options: list[str]) -> None:
    print()
    print(f"  {c(C.VIOLET + C.BOLD, '?')} {c(C.WHITE + C.BOLD, question)}")
    print()
    width = len(str(len(options)))
    for i, opt in enumerate(options, start=1):
        num = c(C.VIOLET, f"{i:>{width}})")
        print(f"    {num} {c(C.WHITE, opt)}")
    print()


def _match_option(raw: str, options: list[str]) -> int | None:
    """Resolve a user input to an option index (1-based). Accepts the
    number, the full option string, or its first word."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        idx = int(raw)
        if 1 <= idx <= len(options):
            return idx
    except ValueError:
        pass
    lower = raw.lower()
    for i, opt in enumerate(options, start=1):
        opt_l = opt.lower()
        if lower == opt_l or lower == opt_l.split()[0].strip("`"):
            return i
    return None


async def _ask_choice(question: str, options: object = None) -> dict:
    """Ask the user to pick one of `options`. Returns the picked value."""
    parsed = _parse_options(options)
    if not parsed:
        return {
            "ok": False,
            "category": "invalid_args",
            "error": "options must be a non-empty list of strings",
        }
    if not isinstance(question, str) or not question.strip():
        return {
            "ok": False,
            "category": "invalid_args",
            "error": "question must be a non-empty string",
        }

    # Pause the spinner so the menu and the input prompt stay visually
    # clean. Restart it on exit so the agent loop keeps its progress UI.
    from ..display.thinking import get_active_indicator
    ind = get_active_indicator()
    spinner_was_running = bool(ind and getattr(ind, "_task", None))
    if spinner_was_running:
        ind.stop()

    try:
        _render_menu(question, parsed)
        prompt = f"  {c(C.VIOLET, '→')} Escolha [1-{len(parsed)}]: "
        attempts = 0
        while attempts < 5:
            try:
                raw = await asyncio.to_thread(input, prompt)
            except (EOFError, KeyboardInterrupt):
                return {
                    "ok": False,
                    "category": "user_cancelled",
                    "error": "user cancelled the choice prompt",
                }
            idx = _match_option(raw, parsed)
            if idx is not None:
                chosen = parsed[idx - 1]
                print(f"  {c(C.GREEN, '✓')} {c(C.GRAY, chosen)}")
                return {
                    "ok": True,
                    "chosen_index": idx,
                    "chosen_value": chosen,
                }
            attempts += 1
            print(
                f"  {c(C.YELLOW, '⚠')} "
                f"Resposta inválida. Digite um número entre 1 e {len(parsed)} "
                f"(ou o texto da opção)."
            )
        return {
            "ok": False,
            "category": "user_cancelled",
            "error": "too many invalid attempts",
        }
    finally:
        if spinner_was_running:
            ind.start()


register_tool(
    ToolDefinition(
        name="ask_choice",
        description=(
            "Ask the user to pick one option from a discrete list. "
            "Use this instead of writing a markdown table or asking the user "
            "to type a category — the REPL renders a numbered menu and "
            "returns the picked value as `chosen_value`. "
            "Examples: choosing an audit category, selecting a target file, "
            "picking a deploy environment. "
            "NOT for free-form text input — the user must pick from `options`."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question shown above the menu.",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of option strings, shown numbered 1..N. "
                        "Keep each option short (one line) — include the "
                        "label and a brief description in the same string, "
                        "e.g. 'security — vulns, secrets, injection'."
                    ),
                },
            },
            "required": ["question", "options"],
        },
        safety=ToolSafety.SAFE,
        executor=_ask_choice,
        category=ToolCategory.GENERAL,
    )
)
