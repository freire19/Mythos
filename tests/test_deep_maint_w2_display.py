"""DEEP_MAINTAINABILITY wave 2 — display layer (#DM069, #DM072, #DM073).

Behavior-preservation tests for the display refactors:
- #DM069a: shared `_pct_color` ladder matches the three legacy inline ladders.
- #DM069b: `card_top`/`card_bottom` reproduce the historical hand-counted
  border strings byte-for-byte.
- #DM069c: single duration formatter (`_format_duration`); `_fmt_duration`
  is gone from cost.py.
- #DM069d: planning.py truncations use `_truncate` (`…` glyph, not `...`).
- #DM072: `ThinkingIndicator.is_scroll_active()` public accessor.
- #DM073: `get_last_turn_summary` / `print_silent_turn` deleted.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout

from alpha.display.theme import (
    C,
    _pct_color,
    _truncate,
    card_bottom,
    card_gutter,
    card_top,
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


class TestPctColor:
    def test_ladder_thresholds(self):
        assert _pct_color(95) == C.RED + C.BOLD
        assert _pct_color(90) == C.RED + C.BOLD
        assert _pct_color(75) == C.YELLOW + C.BOLD
        assert _pct_color(70) == C.YELLOW + C.BOLD
        assert _pct_color(55) == C.YELLOW
        assert _pct_color(50) == C.YELLOW

    def test_low_default_and_override(self):
        # Each legacy site had its own resting color below 50%.
        assert _pct_color(10) == C.GRAY            # prompt chip (core.py)
        assert _pct_color(10, low=C.GREEN) == C.GREEN    # /context bar
        assert _pct_color(10, low=C.YELLOW) == C.YELLOW  # context warning


class TestCardScaffold:
    """The helpers must reproduce the historical hand-counted borders
    byte-for-byte (plain-text comparison; ANSI wrapping is stripped)."""

    def test_cost_stats_preflight_cards_width_43(self):
        legacy = {
            ("COST — current session", 43): "┌─ COST — current session ─────────────────",
            ("PRE-FLIGHT — feedback log", 43): "┌─ PRE-FLIGHT — feedback log ──────────────",
            ("STATS — current session", 43): "┌─ STATS — current session ────────────────",
        }
        for (title, width), expected in legacy.items():
            assert _plain(card_top(title, C.VIOLET, width=width)) == f"  {expected}"
        assert _plain(card_bottom(C.VIOLET, width=43)) == (
            "  └──────────────────────────────────────────"
        )

    def test_plan_card_width_39(self):
        assert _plain(card_top("PLANO PROPOSTO", C.YELLOW, width=39)) == (
            "  ┌─ PLANO PROPOSTO ─────────────────────"
        )
        assert _plain(card_bottom(C.YELLOW, width=39)) == (
            "  └──────────────────────────────────────"
        )

    def test_preflight_card_width_54(self):
        assert _plain(card_top("PRE-FLIGHT", C.YELLOW, width=54)) == (
            "  ┌─ PRE-FLIGHT ────────────────────────────────────────"
        )
        assert _plain(card_bottom(C.YELLOW, width=54)) == (
            "  └─────────────────────────────────────────────────────"
        )

    def test_approval_card_widths_45_41(self):
        assert _plain(card_top("APROVAÇÃO NECESSÁRIA", C.RED, width=45)) == (
            "  ┌─ APROVAÇÃO NECESSÁRIA ─────────────────────"
        )
        assert _plain(card_bottom(C.RED, width=41)) == (
            "  └────────────────────────────────────────"
        )

    def test_gutter(self):
        assert _plain(card_gutter(C.VIOLET)) == "  │"


class TestDurationConsolidation:
    def test_cost_module_has_no_private_formatter(self):
        import alpha.cli.commands.cost as cost_mod

        assert not hasattr(cost_mod, "_fmt_duration")
        # The shared formatter is the one in use now.
        assert cost_mod._format_duration is not None

    def test_shared_format(self):
        from alpha.display import _format_duration

        assert _format_duration(0.5) == ""
        assert _format_duration(45) == "45s"
        assert _format_duration(61) == "1m 1s"
        assert _format_duration(3700) == "1h 1m"


class TestPlanningTruncation:
    def test_todo_list_uses_ellipsis_glyph(self):
        from alpha.display.renderers.planning import _print_todo_list

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_todo_list([{"status": "pending", "content": "x" * 300}])
        out = _plain(buf.getvalue())
        assert "…" in out
        assert "..." not in out
        # _truncate(s, 200) keeps 199 chars + the ellipsis.
        assert "x" * 199 + "…" in out
        assert "x" * 200 not in out

    def test_plan_card_step_uses_ellipsis_glyph(self):
        from alpha.display.renderers.planning import _print_plan_card

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_plan_card({"summary": "s", "steps": ["y" * 300]})
        out = _plain(buf.getvalue())
        assert "…" in out
        assert "..." not in out

    def test_preflight_preview_uses_ellipsis_glyph(self):
        from alpha.display.renderers.planning import _print_preflight_card

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_preflight_card({
                "goal": "g",
                "steps": [{"tool": "t", "args_preview": "z" * 80}],
            })
        out = _plain(buf.getvalue())
        assert _truncate("z" * 80, 40) in out
        assert "..." not in out

    def test_short_content_untouched(self):
        from alpha.display.renderers.planning import _print_todo_list

        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_todo_list([{"status": "completed", "content": "curto"}])
        out = _plain(buf.getvalue())
        assert "curto" in out
        assert "…" not in out


class TestScrollActiveAccessor:
    def test_public_accessor_mirrors_private_flag(self):
        from alpha.display.thinking import ThinkingIndicator

        ind = ThinkingIndicator()
        assert ind.is_scroll_active() is False
        ind._scroll_active = True
        assert ind.is_scroll_active() is True

    def test_renderer_no_longer_touches_private_attr(self):
        import inspect

        from alpha.display.renderers import tools as tools_mod

        src = inspect.getsource(tools_mod)
        assert "._scroll_active" not in src
        assert "is_scroll_active()" in src


class TestDeadCodeRemoved:
    def test_symbols_gone(self):
        import alpha.display as display
        import alpha.display.core as core

        assert not hasattr(core, "get_last_turn_summary")
        assert not hasattr(core, "print_silent_turn")
        assert "get_last_turn_summary" not in display.__all__

    def test_live_neighbors_survive(self):
        # clear_last_turn_summary IS called (cli/entry.py) — must remain.
        from alpha.display import clear_last_turn_summary, print_turn_summary

        assert callable(clear_last_turn_summary)
        assert callable(print_turn_summary)
