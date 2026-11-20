"""AUDIT_V2.2 #028: coverage for interaction_tools parsing/matching helpers.

`_parse_options` and `_match_option` are pure logic at a point of direct user
interaction (ask_choice) with classic off-by-one / ambiguous-match risk, and had
zero tests."""

from alpha.tools.interaction_tools import _match_option, _parse_options


class TestParseOptions:
    def test_list_passthrough_filters_empty(self):
        assert _parse_options(["a", "  ", "b", ""]) == ["a", "b"]

    def test_json_string_array(self):
        assert _parse_options('["x", "y"]') == ["x", "y"]

    def test_non_list_json_returns_empty(self):
        assert _parse_options('{"a": 1}') == []

    def test_invalid_json_returns_empty(self):
        assert _parse_options("not json") == []

    def test_other_types_return_empty(self):
        assert _parse_options(None) == []
        assert _parse_options(42) == []

    def test_coerces_non_str_items(self):
        assert _parse_options([1, 2]) == ["1", "2"]


class TestMatchOption:
    OPTS = ["alpha", "beta", "gamma"]

    def test_match_by_number(self):
        assert _match_option("2", self.OPTS) == 2

    def test_number_out_of_range_returns_none(self):
        assert _match_option("9", self.OPTS) is None
        assert _match_option("0", self.OPTS) is None

    def test_match_by_full_text_case_insensitive(self):
        assert _match_option("GAMMA", self.OPTS) == 3

    def test_match_by_first_word(self):
        assert _match_option("yes", ["yes — keep it", "no — discard"]) == 1

    def test_empty_returns_none(self):
        assert _match_option("   ", self.OPTS) is None

    def test_unknown_text_returns_none(self):
        assert _match_option("delta", self.OPTS) is None

    def test_negative_number_returns_none(self):
        assert _match_option("-1", self.OPTS) is None
