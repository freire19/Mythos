"""Testes da tool web_fetch (search_tools.py).

Cobre: sucesso, conteúdo vazio (SSRF/HTTP fail), esquema inválido,
clamp de max_chars e registro no registry.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from alpha.tools import ToolSafety, get_tool, load_all_tools
from alpha.tools.search_tools import _MAX_CONTEXT_CHARS, _web_fetch


class TestWebFetch:
    @pytest.mark.anyio
    async def test_success(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(return_value="conteudo extraido da pagina"),
        ) as mock_extract:
            result = await _web_fetch("https://example.com/artigo")
        assert result["url"] == "https://example.com/artigo"
        assert result["content"] == "conteudo extraido da pagina"
        assert result["chars"] == len("conteudo extraido da pagina")
        assert result["truncated"] is False
        mock_extract.assert_awaited_once()

    @pytest.mark.anyio
    async def test_empty_content_is_error(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(return_value=""),
        ):
            result = await _web_fetch("https://example.com")
        assert "error" in result
        assert result["url"] == "https://example.com"

    @pytest.mark.anyio
    async def test_rejects_non_http_scheme(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(return_value="nunca chamado"),
        ) as mock_extract:
            for url in ("file:///etc/passwd", "ftp://host/x", "javascript:alert(1)", "etc/passwd"):
                result = await _web_fetch(url)
                assert "error" in result, url
        mock_extract.assert_not_awaited()

    @pytest.mark.anyio
    async def test_max_chars_clamped_to_cap(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(return_value="x"),
        ) as mock_extract:
            await _web_fetch("https://example.com", max_chars=999_999)
        assert mock_extract.await_args.kwargs["max_chars"] == _MAX_CONTEXT_CHARS

    @pytest.mark.anyio
    async def test_truncated_flag_when_at_limit(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(return_value="a" * 100),
        ):
            result = await _web_fetch("https://example.com", max_chars=100)
        assert result["truncated"] is True

    @pytest.mark.anyio
    async def test_extract_exception_becomes_error(self):
        with patch(
            "alpha.web_search.extract_page_content",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await _web_fetch("https://example.com")
        assert result["error"] == "boom"

    def test_registered_as_safe_tool(self):
        load_all_tools()
        tool = get_tool("web_fetch")
        assert tool is not None
        assert tool.safety == ToolSafety.SAFE
        assert "url" in tool.parameters["properties"]
