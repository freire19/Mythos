"""Web search tool for ALPHA agent (direct mode).

Exposes web search as a callable tool so the LLM can search on demand
instead of relying on the pre-loop web search phase.
"""

from __future__ import annotations

import logging

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHARS = 40_000


async def _web_search(
    query: str,
    max_results: int = 5,
    extract_content: bool = True,
) -> dict:
    """Search the web and optionally extract page content."""
    from ..web_search import extract_multiple_pages, search_multiple_queries

    last_error = None
    for attempt in range(2):
        try:
            raw_results = await search_multiple_queries(
                [query], max_results_per_query=min(max_results, 10)
            )
            break
        except Exception as e:
            last_error = e
            if attempt == 0:
                await asyncio.sleep(1.0)
    else:
        return {"results": [], "message": f"Search failed: {last_error}"}

    try:
        if not raw_results:
            return {"results": [], "message": "Nenhum resultado encontrado."}

        results = []
        for r in raw_results[:max_results]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            })

        # Optionally extract full page content
        if extract_content and raw_results:
            urls = [r["href"] for r in raw_results[:6] if r.get("href")]
            url_to_text = await extract_multiple_pages(urls)

            total = 0
            for item in results:
                url = item["url"]
                content = url_to_text.get(url, "")
                if content:
                    remaining = _MAX_CONTEXT_CHARS - total
                    if remaining <= 0:
                        break
                    item["content"] = content[:remaining]
                    total += len(item["content"])

        return {
            "results": results,
            "total": len(results),
        }

    except Exception as e:
        logger.warning(f"web_search tool error: {e}")
        return {"error": str(e)}


async def _web_fetch(url: str, max_chars: int = 20_000) -> dict:
    """Fetch a single URL and extract its main text content."""
    from urllib.parse import urlparse

    from ..web_search import extract_page_content

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        return {"error": f"Esquema não suportado: {scheme or '(vazio)'}. Use http/https."}

    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return {"error": f"max_chars inválido: {max_chars!r} (esperado número inteiro)"}
    max_chars = max(1, min(max_chars, _MAX_CONTEXT_CHARS))
    try:
        content = await extract_page_content(url, max_chars=max_chars)
    except Exception as e:
        logger.warning(f"web_fetch tool error: {e}")
        return {"error": str(e)}

    if not content:
        return {
            "error": (
                "Não foi possível extrair conteúdo da URL "
                "(bloqueada por SSRF, erro HTTP, ou página vazia)."
            ),
            "url": url,
        }

    return {
        "url": url,
        "content": content,
        "chars": len(content),
        "truncated": len(content) >= max_chars,
    }


register_tool(
    ToolDefinition(
        name="web_fetch",
        description=(
            "Fetch a single URL and extract its main text content "
            "(readability-style). Use this to read a specific page the user "
            "or a search result pointed to, without running a new search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "http(s) URL to fetch",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of extracted text "
                        "(default: 20000, max: 40000)"
                    ),
                    "default": 20_000,
                },
            },
            "required": ["url"],
        },
        safety=ToolSafety.SAFE,
        executor=_web_fetch,
        category=ToolCategory.SEARCH,
    )
)


register_tool(
    ToolDefinition(
        name="web_search",
        description=(
            "Search the web using DuckDuckGo and extract page content. "
            "Use this to find current information, answer factual questions, "
            "research topics, or look up documentation. "
            "Returns titles, URLs, snippets, and optionally full page text."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (be specific for better results)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 5, max: 10)",
                    "default": 5,
                },
                "extract_content": {
                    "type": "boolean",
                    "description": "Whether to extract full page content (default: true)",
                    "default": True,
                },
            },
            "required": ["query"],
        },
        safety=ToolSafety.SAFE,
        executor=_web_search,
        category=ToolCategory.SEARCH,
    )
)
