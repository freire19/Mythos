"""DEEP_MAINTAINABILITY wave 2 — #DM068 (single-source command catalogue)
and #DM073-cli (dead code in the CLI command layer).

#DM068: `_DISPATCH`, `/help` and the REPL autocomplete used to keep three
independent command lists that had drifted. Now `iter_commands()` in
`alpha.cli.commands` is the single source; these tests lock in the
consistency invariants so a new command can't silently miss a surface.
"""

from __future__ import annotations

import io as io_module
from contextlib import redirect_stdout

import pytest

from alpha.cli.commands import _ALIASES, _DESCRIPTIONS, _DISPATCH, iter_commands


# Commands handled outside _DISPATCH (they need the raw user_input).
_RAW_INPUT_COMMANDS = {"/image", "/pdf", "/audio"}


class TestCatalogueConsistency:
    def test_every_dispatch_key_is_catalogued_or_alias(self):
        missing = [
            cmd for cmd in _DISPATCH
            if cmd not in _DESCRIPTIONS and cmd not in _ALIASES
        ]
        assert missing == [], f"commands without description/alias: {missing}"

    def test_every_catalogue_entry_is_dispatchable(self):
        ghosts = [
            cmd for cmd in _DESCRIPTIONS
            if cmd not in _DISPATCH and cmd not in _RAW_INPUT_COMMANDS
        ]
        assert ghosts == [], f"catalogued commands with no handler: {ghosts}"

    def test_aliases_point_to_catalogued_commands(self):
        for alias, target in _ALIASES.items():
            assert alias in _DISPATCH, alias
            assert target in _DESCRIPTIONS, f"{alias} -> {target}"
            assert _DISPATCH[alias] is _DISPATCH[target], alias

    def test_iter_commands_shape(self):
        cmds = iter_commands()
        assert cmds == list(_DESCRIPTIONS.items())
        for name, desc in cmds:
            assert name.startswith("/") and " " not in name
            assert desc.strip()

    def test_aliases_hidden_from_catalogue(self):
        names = {name for name, _ in iter_commands()}
        assert names.isdisjoint(_ALIASES)


class TestHelpDerivesFromCatalogue:
    def test_help_lists_every_command_once(self):
        from alpha.cli.commands import DispatchResult
        from alpha.cli.commands.help import _handle_help

        buf = io_module.StringIO()
        with redirect_stdout(buf):
            result = _handle_help(None, ["/help"])
        out = buf.getvalue()
        assert result is DispatchResult.CONTINUE
        for name, desc in iter_commands():
            assert f"{name}" in out and desc in out
        # The /<skill> pseudo-entry survives, and aliases stay hidden.
        assert "/<skill>" in out
        assert "/quit" not in out and "/accept_edits" not in out

    def test_previously_missing_commands_now_listed(self):
        # #DM068 symptom: /preflight was absent from /help.
        names = {name for name, _ in iter_commands()}
        assert {"/preflight", "/rewind", "/tasks", "/plan", "/cost"} <= names


class TestAutocompleteDerivesFromCatalogue:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        import alpha.skills as skills_mod
        from alpha.repl_input import _SlashCompleter

        monkeypatch.setattr(skills_mod, "list_skills", lambda: [])
        _SlashCompleter.invalidate_cache()
        yield
        _SlashCompleter.invalidate_cache()

    def _complete(self, text):
        from prompt_toolkit.document import Document

        from alpha.repl_input import _SlashCompleter

        doc = Document(text, cursor_position=len(text))
        return [c.text for c in _SlashCompleter().get_completions(doc, None)]

    def test_bare_slash_suggests_full_catalogue(self):
        results = self._complete("/")
        expected = {name for name, _ in iter_commands()}
        assert expected <= set(results)

    def test_previously_omitted_commands_now_suggested(self):
        # #DM068 symptom: ~12 real commands were missing from autocomplete.
        results = set(self._complete("/"))
        assert {
            "/accept-edits", "/audio", "/context", "/cost", "/memory",
            "/pdf", "/plan", "/preflight", "/rewind", "/sandbox",
            "/stats", "/tasks",
        } <= results

    def test_cache_format_preserved(self):
        # #DP021 contract: cache entries are (cmd, desc, name_lower).
        from alpha.repl_input import _SlashCompleter

        self._complete("/x")
        cache = _SlashCompleter._entries_cache
        assert cache is not None
        for cmd, desc, name_lower in cache:
            assert name_lower == cmd[1:].lower()
            assert isinstance(desc, str)


class TestDeadCodeRemoved:
    def test_repl_context_has_no_skip_history_record(self):
        # #DM073: field was never read anywhere.
        from alpha.cli.commands import ReplContext

        assert "skip_history_record" not in ReplContext.__dataclass_fields__

    def test_io_module_no_longer_reexports_build_user_content(self):
        # #DM073: the "kept for back-compat" import had no dependents.
        from alpha.cli.commands import io as commands_io

        assert not hasattr(commands_io, "build_user_content")
