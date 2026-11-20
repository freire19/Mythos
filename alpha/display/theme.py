"""
Theme: ANSI colors, display constants, safety icons, simple text utilities.

Extracted from `core.py` as part of the display split (Plano-Upgrade-v3 §1.1).
Pure constants + tiny pure helpers — no side effects, no I/O.
"""

from __future__ import annotations

import os
import shutil
import sys

# ─── Display truncation constants (#D010 V1.0) ───
#
# Antes esses limites viviam inline em ~6 funcoes diferentes (`[:200]`,
# `[:120]`, `[:97]+"..."`, `max_lines = 8`). Centralizar em um lugar
# unico permite ajuste consistente e elimina mismatches silenciosos.
DISPLAY_LINE_TRUNCATE = 200       # max chars per terminal line
DISPLAY_PREVIEW_TRUNCATE = 120    # last-reply preview / TUI status
DISPLAY_PROMPT_VALUE_TRUNCATE = 100  # approval prompt arg values (followed by ...)
# #066: risk-bearing args (command/code/pipeline/content/...) get a much larger
# cap — the user must SEE what they're approving. 100 chars hid most commands.
DISPLAY_PROMPT_RISK_TRUNCATE = 4000
DISPLAY_MAX_LINES = 5             # max lines from a tool result


# ─── ANSI Colors (Kali Linux palette) ───


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    STRIKE = "\033[9m"

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
    MAGENTA = "\033[38;5;75m"     # Navy/steel-blue (sub-agents). Name kept for back-compat with call sites.

    # Navy palette — the Alpha brand color (azul marinho). Replaced the
    # previous violet/purple family. Names kept (VIOLET/VIOLET_GLOW/
    # VIOLET_DARK) to avoid touching ~50 call sites; only the ANSI codes
    # were swapped. VIOLET_GLOW is the breathe-peak shade for the
    # thinking-indicator pulse.
    VIOLET = "\033[38;5;27m"        # #005fff (main brand, electric navy)
    VIOLET_GLOW = "\033[38;5;69m"   # #5f87ff (lifted navy, pulse peak — distinct from BLUE=33)
    VIOLET_DARK = "\033[38;5;25m"   # #005faf (deep navy, borders)

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


# ─── Layout helpers ───

# Result elbow: every tool result hangs off its `● Tool(args)` header as
# `  ⎿  first line`, continuation lines aligned under the text.
ELBOW = "⎿"
RESULT_INDENT = "  "            # before the elbow
RESULT_CONT = "     "           # continuation: indent + elbow + 2 spaces


def term_cols(default: int = 80) -> int:
    """Terminal width in columns (`default` when stdout is not a TTY)."""
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def fit(text: str, used: int = 0) -> str:
    """Trim `text` so a line that already spent `used` columns on its
    prefix never reaches the right edge. A line the terminal has to wrap
    comes back at column 0 and breaks the indentation of the whole block —
    one truncated line reads better than a wrapped one."""
    limit = max(20, min(DISPLAY_LINE_TRUNCATE, term_cols() - used - 1))
    return _truncate(text, limit)


def rel_path(path: str) -> str:
    """Show `path` relative to the working directory when it lives under
    it (`alpha/x.py` instead of the absolute path), else unchanged."""
    if not path or not os.path.isabs(path):
        return path
    try:
        cwd = os.getcwd()
    except OSError:
        return path
    if path == cwd:
        return "."
    prefix = cwd.rstrip(os.sep) + os.sep
    return path[len(prefix):] if path.startswith(prefix) else path


# ─── Shared UI scaffolds (#DM069) ───


def _pct_color(pct: float, low: str = C.GRAY) -> str:
    """Context-usage ladder: pct → color. 90+ critical, 70+ high, 50+ warn;
    below 50 returns `low` (caller-dependent resting color — GRAY for the
    prompt chip, GREEN for the /context bar, YELLOW for warnings)."""
    if pct >= 90:
        return C.RED + C.BOLD
    if pct >= 70:
        return C.YELLOW + C.BOLD
    if pct >= 50:
        return C.YELLOW
    return low


# Default card width matches the /cost · /stats · /preflight cards.
CARD_WIDTH = 43


def card_top(title: str, color: str, width: int = CARD_WIDTH) -> str:
    """`  ┌─ TITLE ────` header line, dashes filled to `width` glyphs.

    Replaces the hand-counted `'┌─ TITLE ──…──'` literals — same visual
    output, but a title/width change no longer requires re-counting dashes.
    """
    bar = f"┌─ {title} "
    return f"  {c(color + C.BOLD, bar + '─' * max(0, width - len(bar)))}"


def card_bottom(color: str, width: int = CARD_WIDTH) -> str:
    """`  └────` footer line closing a card opened by `card_top`."""
    return f"  {c(color + C.BOLD, '└' + '─' * max(0, width - 1))}"


def card_gutter(color: str) -> str:
    """`  │` left-gutter prefix for card body lines."""
    return f"  {c(color, '│')}"
