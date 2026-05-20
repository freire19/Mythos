"""Tests for the `※ recap:` markdown rendering."""

import pytest

from alpha.display import markdown as md
from alpha.display.markdown import _MD_RECAP_RE, _render_recap_line, render_markdown


@pytest.fixture
def colors_on(monkeypatch):
    """Force colors on for render assertions (CI runs without TTY).

    NO_COLOR is imported by value into markdown.py at module-load, so we
    patch the name in *that* module's namespace, not in theme's.
    """
    monkeypatch.setattr(md, "NO_COLOR", False)


class TestRecapRegex:
    def test_matches_simple(self):
        m = _MD_RECAP_RE.search("※ recap: hello world")
        assert m is not None
        assert m.group(1) == "hello world"

    def test_matches_at_end_of_text(self):
        text = "some preamble\n※ recap: final summary"
        m = _MD_RECAP_RE.search(text)
        assert m is not None
        assert m.group(1) == "final summary"

    def test_does_not_match_without_prefix(self):
        assert _MD_RECAP_RE.search("recap: missing the symbol") is None
        assert _MD_RECAP_RE.search("not a recap line at all") is None

    def test_does_not_match_when_indented(self):
        # Must start at line beginning — indented version is body text, not a recap.
        assert _MD_RECAP_RE.search("  ※ recap: nope") is None


class TestRecapRender:
    def test_prefix_styled_cyan_bold(self, colors_on):
        out = _render_recap_line("done")
        assert "\x1b[38;5;37m" in out  # CYAN
        assert "\x1b[1m" in out         # BOLD
        assert "※ recap:" in out

    def test_body_styled_dim_italic(self, colors_on):
        out = _render_recap_line("done")
        assert "\x1b[2m" in out  # DIM
        assert "\x1b[3m" in out  # ITALIC

    def test_no_color_mode_skipped_via_render_markdown(self, monkeypatch):
        monkeypatch.setattr(md, "NO_COLOR", True)
        out = render_markdown("※ recap: nothing styled")
        # In NO_COLOR mode the whole pipeline returns input unchanged
        assert out == "※ recap: nothing styled"

    def test_long_body_wraps_with_hanging_indent(self, colors_on):
        long_body = " ".join(["word"] * 60)
        out = _render_recap_line(long_body)
        # Continuation lines get a 2-space indent
        lines = out.split("\n")
        assert len(lines) > 1
        for line in lines[1:]:
            assert line.startswith("  "), f"expected 2-space indent, got: {line!r}"


class TestRenderMarkdownIntegration:
    def test_recap_in_full_render(self, colors_on):
        text = "Pre-recap body.\n\n※ recap: short summary"
        out = render_markdown(text)
        assert "※ recap:" in out
        assert "\x1b[38;5;37m" in out  # CYAN styling applied
        # Body before recap remains untouched
        assert "Pre-recap body." in out

    def test_recap_preserves_code_span_inside_body(self, colors_on):
        # Backticks in the body should still render as code spans
        text = "※ recap: commit `abc123` landed"
        out = render_markdown(text)
        assert "\x1b[48;5;236m" in out  # BG_GRAY from code span
        assert " abc123 " in out

    def test_non_recap_text_unchanged(self, colors_on):
        text = "Just regular **bold** text."
        out = render_markdown(text)
        # No CYAN+BOLD pair from recap renderer
        # (BOLD alone is fine — appears from bold rendering)
        assert "※ recap:" not in out
