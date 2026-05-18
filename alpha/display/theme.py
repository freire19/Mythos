"""
Theme: ANSI colors, display constants, safety icons, simple text utilities.

Extracted from `core.py` as part of the display split (Plano-Upgrade-v3 §1.1).
Pure constants + tiny pure helpers — no side effects, no I/O.
"""

from __future__ import annotations

import os
import sys

# ─── Display truncation constants (#D010 V1.0) ───
#
# Antes esses limites viviam inline em ~6 funcoes diferentes (`[:200]`,
# `[:120]`, `[:97]+"..."`, `max_lines = 8`). Centralizar em um lugar
# unico permite ajuste consistente e elimina mismatches silenciosos.
DISPLAY_LINE_TRUNCATE = 200       # max chars per terminal line
DISPLAY_PREVIEW_TRUNCATE = 120    # last-reply preview / TUI status
DISPLAY_PROMPT_VALUE_TRUNCATE = 100  # approval prompt arg values (followed by ...)
DISPLAY_MAX_LINES = 8             # max lines from a tool result


# ─── ANSI Colors (Kali Linux palette) ───


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Core palette — solid tones. GREEN_NEON is reserved for the
    # thinking-indicator pulse; everything else stays muted enough that
    # the violet brand reads as the loudest thing on the screen.
    GREEN = "\033[38;5;34m"       # #00af00 forest green
    GREEN_DARK = "\033[38;5;28m"  # #008700
    GREEN_NEON = "\033[38;5;46m"  # #00ff00 (thinking pulse only)
    RED = "\033[38;5;160m"        # #d70000
    RED_DARK = "\033[38;5;124m"   # #af0000
    YELLOW = "\033[38;5;178m"     # #d7af00 gold
    ORANGE = "\033[38;5;172m"     # #d78700
    BLUE = "\033[38;5;33m"        # #0087ff
    CYAN = "\033[38;5;37m"        # #00afaf teal
    MAGENTA = "\033[38;5;135m"    # Purple (sub-agents)

    # Refined violet palette — the Alpha brand color. Picked over the
    # pinker MAGENTA(#af5fff) because the blue undertone reads as alive
    # rather than carnival-bright. VIOLET_GLOW is the breathe-peak shade
    # for the thinking-indicator pulse.
    VIOLET = "\033[38;5;99m"        # #875fff (main brand, electric)
    VIOLET_GLOW = "\033[38;5;141m"  # #af87ff (lavender, pulse peak)
    VIOLET_DARK = "\033[38;5;61m"   # #5f5faf (muted indigo, borders)

    # Soft amber for the accept-edits status chip — distinct from the
    # brand violet so the user can spot the mode at a glance, but
    # desaturated enough not to feel like an alert.
    AMBER_SOFT = "\033[38;5;180m"   # #d7af87 (muted gold)
    WHITE = "\033[38;5;255m"      # Bright white
    GRAY = "\033[38;5;245m"       # Medium gray
    GRAY_DARK = "\033[38;5;238m"  # Dark gray (borders)

    # Backgrounds
    BG_RED = "\033[48;5;52m"      # Dark red background
    BG_GREEN = "\033[48;5;22m"    # Dark green background
    BG_YELLOW = "\033[48;5;58m"   # Dark yellow background
    BG_GRAY = "\033[48;5;236m"    # Dark gray background


def supports_color() -> bool:
    """Check if the terminal supports ANSI color codes."""
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


NO_COLOR = not supports_color()


def c(color: str, text: str) -> str:
    """Wrap text in ANSI color codes. Returns plain text if color is unsupported."""
    if NO_COLOR:
        return text
    return f"{color}{text}{C.RESET}"


# ─── Safety color mapping ───

_SAFETY_COLORS = {
    "safe": C.GREEN,
    "destructive": C.RED,
    "unknown": C.YELLOW,
}

_SAFETY_ICONS = {
    "safe": "●",
    "destructive": "⚠",
    "unknown": "?",
}


def _truncate(s: str, limit: int) -> str:
    """Trim `s` to fit within `limit` characters, marking truncation with `…`."""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"
