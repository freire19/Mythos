"""Model Context Protocol (MCP) integration for Mythos.

Two transports connect to external MCP servers: stdio (`client.py`,
subprocess + reader threads) and Streamable HTTP (`http_client.py`,
sync httpx with `Mcp-Session-Id` sessions). `loader.py` treats both by
duck-typing and auto-registers their tools into the global alpha
TOOL_REGISTRY under the prefix `mcp__<server>__<tool>`.
"""

from __future__ import annotations

from .client import MCPClient, MCPError
from .config import MCPServerConfig, find_config_file, load_mcp_config
from .loader import (
    list_active_servers,
    load_mcp_servers,
    shutdown_mcp_servers,
)

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPServerConfig",
    "find_config_file",
    "list_active_servers",
    "load_mcp_config",
    "load_mcp_servers",
    "shutdown_mcp_servers",
]
