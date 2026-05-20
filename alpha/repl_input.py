"""Rich REPL input built on prompt_toolkit.

Adds two capabilities the builtin `input()` can't provide:

  * Ctrl+V (and Alt+V as a guaranteed fallback) reads images from the
    system clipboard. When an image is found, it's saved to a temp file
    and a `[Image #N]` placeholder is inserted into the buffer.
  * Multiline pastes via bracketed paste are accepted as one submission
    instead of being split into one turn per line.

The function returns `(text, image_paths)`, where `image_paths` is the
list of files referenced by `[Image #N]` placeholders in `text`,
ordered by their numeric index.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import UIContent
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import (
    CompletionsMenuControl,
    _get_menu_item_fragments,
)
from prompt_toolkit.layout.screen import Point
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from ._platform import use_simple_input
from .clipboard import read_image_from_clipboard

logger = logging.getLogger(__name__)

_IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image #(\d+)\]")
_MEDIA_TYPE_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

_temp_image_files: list[Path] = []


def _attach_clipboard_image(buffer, attached: dict[int, Path]) -> bool:
    """Read an image from the clipboard and append a `[Image #N]` to the buffer.

    Returns True if an image was attached, False if the clipboard had no image.
    """
    img = read_image_from_clipboard()
    if img is None:
        return False
    data, media_type = img

    ext = _MEDIA_TYPE_TO_EXT.get(media_type, "png")
    fd = tempfile.NamedTemporaryFile(
        prefix="alpha-clip-", suffix=f".{ext}", delete=False
    )
    try:
        fd.write(data)
    finally:
        fd.close()
    path = Path(fd.name)
    _temp_image_files.append(path)

    n = len(attached) + 1
    attached[n] = path
    placeholder = f"[Image #{n}]"
    if buffer.text and not buffer.text.endswith(" "):
        placeholder = " " + placeholder
    buffer.insert_text(placeholder)
    return True


def _build_key_bindings(attached: dict[int, Path]) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-v")
    def _(event):
        # If clipboard has an image, attach it. Otherwise, fall through to
        # whatever the terminal pastes (in most terminals Ctrl+V never
        # reaches us — text paste is handled by the terminal itself).
        if not _attach_clipboard_image(event.current_buffer, attached):
            # No image in clipboard — let the user know rather than no-op.
            event.app.invalidate()

    @kb.add("escape", "v")  # Alt+V — guaranteed fallback when Ctrl+V is swallowed
    def _(event):
        _attach_clipboard_image(event.current_buffer, attached)

    @kb.add("s-tab")  # Shift+Tab toggles auto-accept-edits mode
    def _(event):
        from .display import toggle_auto_accept

        toggle_auto_accept()
        event.app.invalidate()  # redraw bottom toolbar

    return kb


def _frame_border() -> str:
    """Horizontal rule used as the top/bottom frame around the prompt."""
    from .display import C, c
    cols = max(40, shutil.get_terminal_size((80, 24)).columns - 4)
    return c(C.GRAY_DARK, "─" * cols)


def _bottom_toolbar() -> "ANSI":
    """Bottom frame: closing border, blank spacer, then accept-edits status.
    Matches Claude Code's framed input zone — the prompt sits between this
    block and the top border printed by `read_input` before session.prompt."""
    from .display import C, c, is_auto_accept

    if is_auto_accept():
        status = (
            f" {c(C.AMBER_SOFT + C.BOLD, '»»')} "
            f"{c(C.AMBER_SOFT + C.BOLD, 'accept edits on')} "
            f"{c(C.GRAY, '(shift+tab to cycle) · ctrl+c to interrupt')}"
        )
    else:
        status = (
            f" {c(C.GRAY, '»»')} "
            f"{c(C.GRAY, 'accept edits off')} "
            f"{c(C.GRAY_DARK, '(shift+tab to enable) · ctrl+c to interrupt')}"
        )
    # Bottom border + blank spacer + status (3 lines, prompt_toolkit renders
    # bottom_toolbar as multi-line when ANSI string contains newlines).
    text = f"  {_frame_border()}\n\n{status}"
    return ANSI(text)


def _resolve_placeholders(text: str, attached: dict[int, Path]) -> tuple[str, list[Path]]:
    """Pull out [Image #N] markers from text and return the matching paths."""
    if not attached:
        return text, []
    paths: list[Path] = []
    seen: set[int] = set()
    for match in _IMAGE_PLACEHOLDER_RE.finditer(text):
        idx = int(match.group(1))
        if idx in seen:
            continue
        path = attached.get(idx)
        if path and path.exists():
            paths.append(path)
            seen.add(idx)
    return text, paths


_BUILTIN_COMMANDS: list[tuple[str, str]] = [
    ("/init", "Draft an ALPHA.md for this project"),
    ("/clear", "Clear history and screen"),
    ("/history", "Show conversation history"),
    ("/save", "Save current session"),
    ("/load", "Load a previous session"),
    ("/continue", "Resume from last session"),
    ("/sessions", "List saved sessions"),
    ("/tools", "List available tools"),
    ("/skills", "List registered skills (ready vs inactive)"),
    ("/mcp", "List connected MCP servers"),
    ("/image", "Attach an image (Ctrl+V also works)"),
    ("/agents", "List named agents"),
    ("/agent", "Show/switch active agent"),
    ("/model", "Show/switch provider & model"),
    ("/help", "Show all commands"),
    ("/exit", "Exit"),
]


class _SlashCompleter(Completer):
    """Autocomplete for ``/command`` and ``/<skill-name>``.

    Triggers only when the line is a single slash-token with no whitespace
    yet — that's the only place where typing a name matters. Once the user
    hits space, completion stops so it doesn't compete with normal text.
    """

    # DEEP_PERFORMANCE #D029: cache de entries para evitar list_skills()
    # (que itera o filesystem) a cada keystroke. Invalidado quando skills
    # são recarregadas via invalidate_skill_entries_cache().
    _entries_cache: list[tuple[str, str]] | None = None

    @classmethod
    def invalidate_cache(cls) -> None:
        cls._entries_cache = None

    def get_completions(self, document, complete_event):
        line = document.text_before_cursor
        if not line.startswith("/") or " " in line:
            return

        # Skills are imported lazily so this module stays import-cheap and
        # doesn't pull the registry at definition time.
        if _SlashCompleter._entries_cache is None:
            entries: list[tuple[str, str]] = list(_BUILTIN_COMMANDS)
            try:
                from .skills import list_skills
                for s in list_skills():
                    meta = (s.description or "").strip().split("\n", 1)[0]
                    entries.append((f"/{s.name}", meta[:80] or "skill"))
            except Exception as e:
                # #DM043: silencioso engole skill listing — debug log preserva
                # diagnostico sem poluir terminal (skills sao opcionais).
                logger.debug("skill list failed for slash completer: %s", e)
            _SlashCompleter._entries_cache = entries
        else:
            entries = _SlashCompleter._entries_cache

        # Substring match with prefix-first ranking: typing `/save` matches
        # both `/save-anything` (prefix) and `/git-save` (substring). Prefix
        # hits stream first so the closest match is at the top of the popup.
        needle = line[1:].lower()
        if not needle:
            ordered = entries
        else:
            prefix_hits: list[tuple[str, str]] = []
            substr_hits: list[tuple[str, str]] = []
            for cmd, desc in entries:
                name = cmd[1:].lower()
                if name.startswith(needle):
                    prefix_hits.append((cmd, desc))
                elif needle in name:
                    substr_hits.append((cmd, desc))
            ordered = prefix_hits + substr_hits

        for cmd, desc in ordered:
            yield Completion(
                cmd,
                start_position=-len(line),
                display_meta=desc,
            )


class _WrappedCompletionsMenuControl(CompletionsMenuControl):
    """Replaces the default single-line truncated meta column with a
    Claude-Code-style multi-line word-wrapped description.

    Each completion entry can span up to ``MAX_META_LINES`` lines; longer
    descriptions are truncated at a word boundary with a U+2026 ellipsis.
    The selection background extends across all lines of the active entry.
    """

    META_TARGET_WIDTH = 40
    MAX_META_LINES = 3

    def _show_meta(self, complete_state) -> bool:
        return any(c.display_meta_text for c in complete_state.completions)

    def _get_menu_meta_width(self, max_width: int, complete_state) -> int:
        if not self._show_meta(complete_state):
            return 0
        return max(8, min(max_width, self.META_TARGET_WIDTH))

    def _wrap_text(self, text: str, width: int) -> list[str]:
        if not text or width <= 0:
            return [""]
        words = text.split()
        if not words:
            return [""]
        lines: list[str] = []
        current = ""
        idx = 0
        while idx < len(words) and len(lines) < self.MAX_META_LINES:
            w = words[idx]
            cand = (current + " " + w) if current else w
            if get_cwidth(cand) <= width:
                current = cand
                idx += 1
            elif current:
                lines.append(current)
                current = ""
            else:
                cut = max(1, width - 1)
                lines.append(w[:cut] + "…")
                idx += 1
        if current and len(lines) < self.MAX_META_LINES:
            lines.append(current)
        if idx < len(words) and lines:
            last = lines[-1]
            while last and get_cwidth(last) > width - 1:
                last = last[:-1].rstrip()
            lines[-1] = (last + "…") if last else "…"
        return lines or [""]

    def _line_layout(self, complete_state, menu_width: int, menu_meta_width: int):
        flat: list[tuple[int, int, bool, str]] = []
        cursor_y = 0
        index = complete_state.complete_index
        show_meta = self._show_meta(complete_state) and menu_meta_width > 4
        for ci, c in enumerate(complete_state.completions):
            is_current = ci == index
            meta = (c.display_meta_text or "").strip() if show_meta else ""
            wrapped = self._wrap_text(meta, menu_meta_width - 2) if meta else [""]
            if ci == index:
                cursor_y = len(flat)
            for li, line_text in enumerate(wrapped):
                flat.append((ci, li, is_current, line_text))
        return flat, cursor_y, show_meta

    def preferred_height(self, width, max_available_height, wrap_lines, get_line_prefix):
        complete_state = get_app().current_buffer.complete_state
        if not complete_state:
            return 0
        menu_width = self._get_menu_width(width, complete_state)
        menu_meta_width = self._get_menu_meta_width(width - menu_width, complete_state)
        flat, _, _ = self._line_layout(complete_state, menu_width, menu_meta_width)
        return len(flat)

    def create_content(self, width: int, height: int) -> UIContent:
        complete_state = get_app().current_buffer.complete_state
        if not complete_state:
            return UIContent()

        menu_width = self._get_menu_width(width, complete_state)
        menu_meta_width = self._get_menu_meta_width(width - menu_width, complete_state)
        flat, cursor_y, show_meta = self._line_layout(complete_state, menu_width, menu_meta_width)

        # Mouse handler needs to map row → completion index when the user
        # clicks any line of a multi-line entry.
        self._line_to_completion = [t[0] for t in flat]

        completions = complete_state.completions

        def get_line(y):
            ci, li, is_current, meta_line = flat[y]
            c = completions[ci]
            if li == 0:
                name_frags = _get_menu_item_fragments(
                    c, is_current, menu_width, space_after=True
                )
            else:
                style = (
                    "class:completion-menu.completion.current"
                    if is_current
                    else "class:completion-menu.completion"
                )
                name_frags = [(style, " " * menu_width)]

            if show_meta:
                style = (
                    "class:completion-menu.meta.completion.current"
                    if is_current
                    else "class:completion-menu.meta.completion"
                )
                pad = " " * max(0, menu_meta_width - 1 - get_cwidth(meta_line))
                meta_frags = to_formatted_text(
                    [("", " " + meta_line + pad)], style=style
                )
            else:
                meta_frags = []
            return name_frags + meta_frags

        return UIContent(
            get_line=get_line,
            cursor_position=Point(x=0, y=cursor_y),
            line_count=len(flat),
        )

    def mouse_handler(self, mouse_event):
        b = get_app().current_buffer
        y = mouse_event.position.y
        mapping = getattr(self, "_line_to_completion", None)
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            if mapping and 0 <= y < len(mapping):
                b.go_to_completion(mapping[y])
                b.complete_state = None
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            b.complete_next(count=3, disable_wrap_around=True)
        elif mouse_event.event_type == MouseEventType.SCROLL_UP:
            b.complete_previous(count=3, disable_wrap_around=True)
        return None


def _patch_completions_menu(session: PromptSession) -> None:
    try:
        for c in session.app.layout.walk():
            if (
                isinstance(c, Window)
                and isinstance(c.content, CompletionsMenuControl)
                and not isinstance(c.content, _WrappedCompletionsMenuControl)
            ):
                c.content = _WrappedCompletionsMenuControl()
                c.height = Dimension(min=1, max=30)
                return
    except Exception:
        logger.debug("completions-menu patch failed; falling back to default", exc_info=True)


_SESSION: PromptSession | None = None


# Style applied to the user-typed text so it's visually distinct from the
# prompt arrow and from agent output. Bright neon green + bold mirrors the
# Alpha brand and matches the indicator/prompt arrow tint.
_INPUT_STYLE = Style.from_dict({
    "": "fg:#5fff5f bold",  # the empty class styles unstyled buffer text
    # prompt_toolkit gives bottom-toolbar a reversed bg by default, which
    # turns our ANSI greens into hard-to-read fg-on-light. Force a dark bg
    # + neutral fg so the embedded ANSI escapes (from _bottom_toolbar) win.
    "bottom-toolbar": "bg:#1a1a1a fg:#cccccc noreverse",
    "bottom-toolbar.text": "bg:#1a1a1a fg:#cccccc noreverse",
    # Slash-autocomplete menu — match Claude Code's look: terminal-dark bg,
    # discreet text colors, subtle selection highlight (no bright gray bar).
    "completion-menu": "bg:#0c0c0c",
    "completion-menu.completion": "bg:#0c0c0c fg:#d0d0d0",
    "completion-menu.completion.current": "bg:#2a2a2a fg:#ffffff bold",
    "completion-menu.meta.completion": "bg:#0c0c0c fg:#808080",
    "completion-menu.meta.completion.current": "bg:#2a2a2a fg:#cccccc",
    # Scrollbar of the autocomplete popup — subdue it.
    "scrollbar.background": "bg:#0c0c0c",
    "scrollbar.button": "bg:#3a3a3a",
})


def _get_session() -> PromptSession:
    global _SESSION
    if _SESSION is None:
        _SESSION = PromptSession(
            completer=_SlashCompleter(),
            complete_while_typing=True,
            bottom_toolbar=_bottom_toolbar,
            style=_INPUT_STYLE,
            include_default_pygments_style=False,
        )
        _patch_completions_menu(_SESSION)
    return _SESSION


def _read_input_simple(prompt_ansi: str) -> tuple[str, list[Path]]:
    """Fallback `input()` pra Windows legacy (conhost antigo, PowerShell ISE)
    onde o framed zone do prompt_toolkit nao renderiza e o usuario digita
    no escuro. Perde Ctrl+V e shift+tab toggle, mas funciona.
    """
    sys.stdout.write("\n")
    sys.stdout.flush()
    try:
        text = input(prompt_ansi)
    except UnicodeDecodeError:
        # Codepage exotico (cp437 etc.) com bytes nao decodificaveis pelo
        # locale; re-le como bytes brutos com decode lossy.
        text = sys.stdin.readline().rstrip("\n")
    return text, []


def read_input(prompt_ansi: str) -> tuple[str, list[Path]]:
    """Read a line from the user. Returns (text, image_paths).

    Raises EOFError on Ctrl+D and KeyboardInterrupt on Ctrl+C — same as
    the builtin `input()`.
    """
    if use_simple_input():
        return _read_input_simple(prompt_ansi)

    # Top frame border + spacer above the prompt — pairs with the bottom
    # border emitted via `_bottom_toolbar` to give the input area a
    # demarcated "input box" feel (mirrors Claude Code's frame).
    sys.stdout.write(f"\n  {_frame_border()}\n")
    sys.stdout.flush()

    attached: dict[int, Path] = {}
    kb = _build_key_bindings(attached)
    session = _get_session()
    text = session.prompt(ANSI(prompt_ansi), key_bindings=kb)
    return _resolve_placeholders(text, attached)


def cleanup_temp_images() -> None:
    """Remove temp clipboard images. Call from atexit."""
    for path in _temp_image_files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _temp_image_files.clear()
