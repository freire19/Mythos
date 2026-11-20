"""DEEP_PERFORMANCE wave 2 — hot-path micro-otimizações (#DP043, #DP044, #DP038, #DP021).

Testes focados em comportamento preservado: os fixes são de custo, não de
semântica — cada teste trava o resultado observável e, quando barato, a
propriedade de performance (contagem de chamadas / bounded input).
"""

import difflib

import pytest

from alpha._text_safety import strip_control_and_bidi
from alpha.mcp.loader import _sanitize_mcp_uri
from alpha.tools._delegate_core import _strip_control_chars
from alpha.tools.delegate_tools import _cluster_answers


# ── #DP043 — _strip_control_chars (já resolvido via _text_safety #501/#DP024) ──


class TestStripControlChars:
    def test_removes_control_and_bidi(self):
        assert _strip_control_chars("a\x00b\x1bc‮d") == "abcd"

    def test_preserves_tab_newline_cr(self):
        assert _strip_control_chars("a\tb\nc\rd") == "a\tb\nc\rd"

    def test_removes_del(self):
        assert _strip_control_chars("a\x7fb") == "ab"

    def test_empty_string(self):
        assert _strip_control_chars("") == ""

    def test_delegates_to_shared_policy(self):
        sample = "x\x00‪؜y"
        assert _strip_control_chars(sample) == strip_control_and_bidi(sample)

    def test_table_is_module_level_constant(self):
        from alpha import _text_safety
        table = _text_safety._CONTROL_AND_BIDI_TABLE
        assert isinstance(table, dict)
        assert table[0] is None
        assert table[0x1B] is None
        assert table[0x7F] is None
        assert table[ord("‮")] is None
        assert 9 not in table and 10 not in table and 13 not in table


# ── #DP044 — clustering do delegate_consensus ──


class TestClusterAnswers:
    def test_identical_answers_one_cluster(self):
        assert _cluster_answers(["yes", "yes", "yes"]) == [[0, 1, 2]]

    def test_distinct_answers_separate_clusters(self):
        clusters = _cluster_answers(["aaaaaaaaaa", "zzzzzzzzzz" * 3])
        assert clusters == [[0], [1]]

    def test_similar_answers_cluster_together(self):
        a = "The bug is in the parser module, line 42."
        b = "The bug is in the parser module, line 43."
        clusters = _cluster_answers([a, b])
        assert clusters == [[0, 1]]

    def test_empty_answers_share_degenerate_cluster(self):
        clusters = _cluster_answers(["", "   ", "real answer here"])
        assert clusters == [[0, 1], [2]]

    def test_all_empty(self):
        assert _cluster_answers(["", None and "" or ""]) == [[0, 1]]

    def test_matches_reference_implementation(self):
        answers = [
            "the function returns None on error",
            "the function returns None when an error occurs",
            "it raises ValueError instead",
            "",
            "the function returns None on error!",
        ]
        # Referência: clustering greedy original (matcher fresco + ratio cheio).
        empty_indices = []
        non_empty = []
        for idx, ans in enumerate(answers):
            norm = (ans or "").strip()[:4000]
            (non_empty.append((idx, norm)) if norm else empty_indices.append(idx))
        expected = []
        reps = []
        if empty_indices:
            expected.append(empty_indices)
            reps.append("")
        for idx, norm in non_empty:
            placed = False
            for ci, rep in enumerate(reps):
                if not rep:
                    continue
                if difflib.SequenceMatcher(autojunk=False, a=rep, b=norm).ratio() >= 0.7:
                    expected[ci].append(idx)
                    placed = True
                    break
            if not placed:
                expected.append([idx])
                reps.append(norm)
        assert _cluster_answers(answers) == expected

    def test_quick_ratio_gate_skips_full_ratio(self, monkeypatch):
        from alpha.tools import delegate_tools

        calls = {"ratio": 0}
        real_matcher = difflib.SequenceMatcher

        class SpyMatcher(real_matcher):
            def ratio(self):
                calls["ratio"] += 1
                return super().ratio()

        class SpyDifflib:
            SequenceMatcher = SpyMatcher

        monkeypatch.setattr(delegate_tools, "difflib", SpyDifflib)
        # Sem char em comum → real_quick_ratio = 0 → ratio() nunca roda.
        _cluster_answers(["aaaa" * 100, "zzzz" * 100])
        assert calls["ratio"] == 0

    async def test_consensus_offloads_clustering(self, monkeypatch):
        from alpha.tools import delegate_tools

        async def fake_run_subagent(task, context, tools_filter, provider, **kw):
            return {"status": "completed", "result": "answer 42"}

        monkeypatch.setattr(delegate_tools, "_run_subagent", fake_run_subagent)
        monkeypatch.setattr(
            delegate_tools, "_check_delegation_enabled", lambda: None
        )
        result = await delegate_tools._delegate_consensus("q?", n=2)
        assert result["ok"] is True
        assert result["consensus"]["reached"] is True
        assert result["consensus"]["majority"]["size"] == 2


# ── #DP038 — _sanitize_mcp_uri bounded scan ──


class TestSanitizeMcpUri:
    def test_clean_short_uri_unchanged(self):
        assert _sanitize_mcp_uri("file:///a/b.txt") == "file:///a/b.txt"

    def test_empty(self):
        assert _sanitize_mcp_uri("") == ""

    def test_strips_control_and_newlines(self):
        assert _sanitize_mcp_uri("http://x\n\r\t\x00y") == "http://xy"

    def test_exactly_512_no_marker(self):
        uri = "a" * 512
        assert _sanitize_mcp_uri(uri) == uri

    def test_over_512_truncated_with_marker(self):
        out = _sanitize_mcp_uri("a" * 600)
        assert out == "a" * 512 + "...[uri-truncated]"

    def test_huge_uri_bounded_output(self):
        out = _sanitize_mcp_uri("b" * 10_000_000)
        assert out == "b" * 512 + "...[uri-truncated]"
        assert len(out) <= 512 + len("...[uri-truncated]")

    def test_huge_uri_scan_is_bounded(self, monkeypatch):
        # O scan char-a-char não pode tocar além do head de 513 chars:
        # chars inválidos depois do cap não afetam (nem custam) nada.
        uri = "c" * 513 + "\x00" * 9_000_000
        assert _sanitize_mcp_uri(uri) == "c" * 512 + "...[uri-truncated]"


# ── #DP021 — _SlashCompleter cache com nome pré-lowercased ──


class TestSlashCompleter:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self, monkeypatch):
        from alpha.repl_input import _SlashCompleter
        import alpha.skills as skills_mod

        class FakeSkill:
            name = "Git-Save"
            description = "Commit + push"

        monkeypatch.setattr(skills_mod, "list_skills", lambda: [FakeSkill()])
        _SlashCompleter.invalidate_cache()
        yield
        _SlashCompleter.invalidate_cache()

    def _complete(self, text):
        from prompt_toolkit.document import Document
        from alpha.repl_input import _SlashCompleter

        doc = Document(text, cursor_position=len(text))
        return [c.text for c in _SlashCompleter().get_completions(doc, None)]

    def test_prefix_match(self):
        assert self._complete("/sav") == ["/save", "/Git-Save"]

    def test_substring_after_prefix(self):
        results = self._complete("/save")
        assert results[0] == "/save"
        assert "/Git-Save" in results

    def test_case_insensitive_skill_match(self):
        assert "/Git-Save" in self._complete("/git-s")

    def test_bare_slash_lists_all(self):
        results = self._complete("/")
        assert "/help" in results and "/exit" in results and "/Git-Save" in results

    def test_no_completion_after_space(self):
        assert self._complete("/save foo") == []

    def test_cache_stores_prelowered_names(self):
        from alpha.repl_input import _SlashCompleter

        self._complete("/x")
        cache = _SlashCompleter._entries_cache
        assert cache is not None
        for cmd, desc, name_lower in cache:
            assert name_lower == cmd[1:].lower()

    def test_invalidate_cache_resets(self):
        from alpha.repl_input import _SlashCompleter

        self._complete("/x")
        assert _SlashCompleter._entries_cache is not None
        _SlashCompleter.invalidate_cache()
        assert _SlashCompleter._entries_cache is None
