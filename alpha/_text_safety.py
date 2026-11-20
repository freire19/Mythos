"""Política única de sanitização de control chars e bidi-overrides.

AUDIT_V2.3 #501: a mesma política de segurança ("quais codepoints são
perigosos em texto que o LLM lê como contexto") existia em três
implementações divergentes — `_delegate_core._strip_control_chars`,
`mcp/loader._sanitize_mcp_text` e `executor._strip_bidi` — com o set de
bidi-overrides copiado literal entre as duas primeiras e U+061C (ALM)
presente apenas na terceira. Este módulo é a fonte única da verdade.

`BIDI_OVERRIDES` é a UNIÃO dos três sets (superset, incluindo U+061C).

IMPORTANTE — exclusão deliberada de ZWNJ/ZWJ (U+200C/U+200D): o
neutralizador de close-tag do executor (`</tool_result>` → tag quebrada)
USA ZWNJ; stripá-lo aqui desfaria essa neutralização. Nenhuma das duas
funções abaixo remove ZWNJ/ZWJ — não "unifique" isso de volta.

Perf (#DP024): translation tables module-level → um único passe C via
`str.translate`, que retorna a própria string quando nada casa.
"""

from __future__ import annotations

# LRE/RLE/PDF/LRO/RLO · LRI/RLI/FSI/PDI · LRM/RLM · ALM (U+061C)
BIDI_OVERRIDES = frozenset({
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩",
    "‎", "‏",
    "؜",
})

# ASCII control: tudo abaixo de 0x20 exceto \t \n \r, mais DEL (0x7f)
_CONTROL_CHARS = frozenset(
    chr(c) for c in range(32) if c not in (9, 10, 13)
) | {"\x7f"}

_CONTROL_AND_BIDI_TABLE = {ord(c): None for c in (_CONTROL_CHARS | BIDI_OVERRIDES)}
_BIDI_TABLE = {ord(c): None for c in BIDI_OVERRIDES}


def strip_control_and_bidi(text: str) -> str:
    """Remove ASCII control (NUL, ESC/ANSI, …) + bidi/format overrides.

    Uso: texto não-confiável inserido em prompts — subagent.md e task de
    `delegate_*` (`_delegate_core`), resultados/descrições MCP (`loader`).
    Preserva \\t \\n \\r.
    """
    if not text:
        return text
    return text.translate(_CONTROL_AND_BIDI_TABLE)


def strip_bidi(text: str) -> str:
    """Remove apenas bidi/format overrides (inclui U+061C), preservando
    control chars.

    Uso: `executor._format_result`, onde o texto já foi serializado por
    `json.dumps` (controls viram escapes) e ZWNJ do neutralizador de
    close-tag precisa sobreviver.
    """
    if not text:
        return text
    return text.translate(_BIDI_TABLE)
