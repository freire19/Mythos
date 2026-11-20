"""AUDIT_V2.3 #501 — política única de control/bidi em `alpha._text_safety`.

E #102 — cap de profundidade do `_sanitize_schema` do loader MCP.
"""

from __future__ import annotations

from alpha._text_safety import BIDI_OVERRIDES, strip_bidi, strip_control_and_bidi

ALM = "؜"
RLO = "‮"
ZWNJ = "‌"
ZWJ = "‍"


class TestUnifiedPolicy:
    def test_superset_includes_alm(self):
        # Nota da review: a união DEVE incluir U+061C — só o executor cobria.
        assert ALM in BIDI_OVERRIDES

    def test_control_and_bidi_strips_alm(self):
        # Antes do #501, _delegate_core e loader deixavam ALM passar.
        assert strip_control_and_bidi(f"a{ALM}b{RLO}c") == "abc"

    def test_control_and_bidi_strips_nul_ansi_keeps_whitespace(self):
        assert strip_control_and_bidi("a\x00b\x1b[31mc\td\ne\rf\x7f") == "ab[31mc\td\ne\rf"

    def test_strip_bidi_preserves_controls(self):
        # Variante do executor: só bidi — o texto já passou por json.dumps.
        assert strip_bidi(f"a\x00{RLO}b{ALM}") == "a\x00b"

    def test_zwnj_zwj_never_stripped(self):
        # O neutralizador de </tool_result> USA ZWNJ — stripar desfaria isso.
        s = f"a{ZWNJ}b{ZWJ}c"
        assert strip_bidi(s) == s
        assert strip_control_and_bidi(s) == s

    def test_empty_and_clean_passthrough(self):
        assert strip_control_and_bidi("") == ""
        assert strip_bidi("") == ""
        clean = "texto normal, com acentuação e \n quebras"
        assert strip_control_and_bidi(clean) == clean


class TestSchemaDepthCap:
    def test_deeply_nested_schema_does_not_recurse_unbounded(self):
        # #102: schema patologicamente aninhado não pode estourar a recursão.
        from alpha.mcp.loader import _sanitize_schema

        deep = {"type": "object"}
        for _ in range(50_000):
            deep = {"properties": deep}
        out = _sanitize_schema(deep)  # não pode levantar RecursionError
        assert isinstance(out, dict)

    def test_shallow_schema_fully_sanitized(self):
        from alpha.mcp.loader import _sanitize_schema

        schema = {
            "type": "object",
            "properties": {
                "x": {"description": f"faça{RLO} isto", "enum": [f"a{ALM}b"]},
            },
        }
        out = _sanitize_schema(schema)
        assert out["properties"]["x"]["description"] == "faça isto"
        assert out["properties"]["x"]["enum"] == ["ab"]
