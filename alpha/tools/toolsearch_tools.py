"""`tool_search` — pulls deferred tool schemas into the prompt on demand.

The real schema the model sees is built dynamically by
`alpha.tool_lazy.build_tool_search_schema` (its description is the live
index of deferred tools). This registration exists so the executor can
dispatch the call through the normal registry path (SAFE → auto-run) and
so `get_tool("tool_search")` resolves for approval/plan-mode checks.

`get_openai_tools()` must NOT advertise this static entry — `apply()`
drops any pre-existing `tool_search` entry and appends the dynamic one,
so the static description here is only a fallback.
"""

from __future__ import annotations

from typing import Any

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool


async def _tool_search(query: str, max_results: int | None = None) -> dict[str, Any]:
    from ..tool_lazy import search
    return search(query, max_results)


register_tool(
    ToolDefinition(
        name="tool_search",
        description=(
            "Load the full schema of deferred tools (browser_*, database, "
            "scraping, large MCP servers…) so you can call them. "
            "`select:name1,name2` or keywords."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        safety=ToolSafety.SAFE,
        executor=_tool_search,
        category=ToolCategory.GENERAL,
    )
)
