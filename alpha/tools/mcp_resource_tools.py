"""MCP resources — `mcp_list_resources` / `mcp_read_resource`.

Parity wave 2026-11, item 9. MCP servers expose read-only *resources*
(files, DB rows, docs — `resources/list` + `resources/read`) next to their
tools; the loader only ever registered tools. These two SAFE tools give
the model the resource side of every connected server without a per-
server tool explosion: one `list` to discover URIs, one `read` to fetch.

Safety: output goes through the same control/bidi stripping as MCP tool
results, URIs are sanitized, text is capped (`_READ_CAP`) and binary blobs
are reported by size only. Both calls run the blocking client in a thread
with the same turn-budget clamp as MCP tool calls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import ToolCategory, ToolDefinition, ToolSafety, register_tool

_READ_CAP = 40_000
_LIST_CAP = 200


def _sanitize():
    from ..mcp.loader import _sanitize_mcp_text, _sanitize_mcp_uri
    return _sanitize_mcp_text, _sanitize_mcp_uri


def _resolve(server: str):
    from ..mcp.loader import get_client, list_active_servers

    client = get_client(server)
    if client is None:
        names = [s["name"] for s in list_active_servers()]
        return None, {
            "ok": False, "category": "not_found",
            "error": f"MCP server '{server}' não conectado. Ativos: {names or 'nenhum'}.",
        }
    if not client.supports_resources():
        return None, {
            "ok": False, "category": "unsupported",
            "error": f"MCP server '{server}' não declara a capability `resources`.",
        }
    return client, None


def _timeout() -> float:
    from .._turn_budget import remaining
    from ..mcp.client import TOOL_CALL_TIMEOUT

    left = remaining()
    return TOOL_CALL_TIMEOUT if left is None else max(1.0, min(TOOL_CALL_TIMEOUT, left))


async def _mcp_list_resources(server: str, cursor: str = "") -> dict[str, Any]:
    from ..mcp.client import MCPError

    client, err = _resolve(server)
    if err:
        return err
    san_text, san_uri = _sanitize()
    try:
        raw = await asyncio.to_thread(client.list_resources, cursor or None)
    except MCPError as e:
        return {"ok": False, "category": "mcp_error", "error": str(e)}
    items = []
    for r in (raw.get("resources") or [])[:_LIST_CAP]:
        if not isinstance(r, dict):
            continue
        items.append({
            "uri": san_uri(str(r.get("uri", ""))),
            "name": san_text(str(r.get("name", "")))[:200],
            "description": san_text(str(r.get("description", "")))[:500],
            "mimeType": san_uri(str(r.get("mimeType", "")))[:100],
        })
    out: dict[str, Any] = {"ok": True, "server": server, "count": len(items), "resources": items}
    if raw.get("nextCursor"):
        out["nextCursor"] = san_uri(str(raw["nextCursor"]))
    return out


async def _mcp_read_resource(server: str, uri: str) -> dict[str, Any]:
    from ..mcp.client import MCPError

    if not isinstance(uri, str) or not uri.strip():
        return {"ok": False, "category": "invalid_args", "error": "uri vazia."}
    client, err = _resolve(server)
    if err:
        return err
    san_text, san_uri = _sanitize()
    try:
        raw = await asyncio.to_thread(client.read_resource, uri.strip(), _timeout())
    except MCPError as e:
        return {"ok": False, "category": "mcp_error", "error": str(e)}
    parts: list[str] = []
    binary = 0
    for item in raw.get("contents") or []:
        if not isinstance(item, dict):
            continue
        label = san_uri(str(item.get("uri", "")))
        if "text" in item:
            parts.append(f"[resource {label}]\n{san_text(str(item.get('text', '')))}")
        elif "blob" in item:
            binary += 1
            parts.append(f"[resource {label}: binary, {len(str(item.get('blob', '')))} base64 chars omitted]")
    text = "\n\n".join(parts)
    truncated = len(text) > _READ_CAP
    if truncated:
        text = text[:_READ_CAP] + "\n…[truncated]"
    return {
        "ok": True, "server": server, "uri": san_uri(uri), "content": text,
        "parts": len(parts), "binary_parts": binary, "truncated": truncated,
    }


register_tool(
    ToolDefinition(
        name="mcp_list_resources",
        description=(
            "List the read-only resources (files, records, docs) exposed by a connected "
            "MCP server, with their URIs. Use `mcp_read_resource` to fetch one. "
            "Only servers that declare the `resources` capability."
        ),
        parameters={
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name (see /mcp)."},
                "cursor": {"type": "string", "description": "Pagination cursor from a previous call.", "default": ""},
            },
            "required": ["server"],
        },
        safety=ToolSafety.SAFE,
        executor=_mcp_list_resources,
        category=ToolCategory.NETWORK,
    )
)

register_tool(
    ToolDefinition(
        name="mcp_read_resource",
        description=(
            "Read one resource from a connected MCP server by URI (from mcp_list_resources). "
            "Text is returned sanitized and capped; binary blobs are reported by size."
        ),
        parameters={
            "type": "object",
            "properties": {
                "server": {"type": "string", "description": "MCP server name."},
                "uri": {"type": "string", "description": "Resource URI."},
            },
            "required": ["server", "uri"],
        },
        safety=ToolSafety.SAFE,
        executor=_mcp_read_resource,
        category=ToolCategory.NETWORK,
    )
)
