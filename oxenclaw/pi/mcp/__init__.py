"""MCP (Model Context Protocol) client for oxenClaw.

Lets oxenClaw consume external MCP servers as if their tools were native
oxenClaw tools. Mirrors openclaw's `src/agents/mcp-*` + `pi-bundle-mcp-*`
modules. Server-side (exposing oxenClaw tools as an MCP server) is a
separate, future phase.

Public API:

- `MCPServerConfig`, `parse_server_config` — config parsing
- `MCPClient`, `MCPClientPool` — connection + JSON-RPC client
- `materialize_mcp_tools` — turn an MCPClientPool into oxenClaw `Tool`s
- `sanitize_server_name`, `build_safe_tool_name` — name shaping
"""

from oxenclaw.pi.mcp.adapter import materialize_mcp_tools
from oxenclaw.pi.mcp.client import MCPClient, MCPClientPool, MCPError
from oxenclaw.pi.mcp.config import (
    HttpServerConfig,
    MCPServerConfig,
    StdioServerConfig,
    parse_server_config,
)
from oxenclaw.pi.mcp.loader import build_pool_from_config, load_mcp_configs
from oxenclaw.pi.mcp.names import (
    TOOL_NAME_SEPARATOR,
    build_safe_tool_name,
    sanitize_server_name,
    sanitize_tool_name,
)

__all__ = [
    "TOOL_NAME_SEPARATOR",
    "HttpServerConfig",
    "MCPClient",
    "MCPClientPool",
    "MCPError",
    "MCPServerConfig",
    "StdioServerConfig",
    "build_pool_from_config",
    "build_safe_tool_name",
    "load_mcp_configs",
    "materialize_mcp_tools",
    "parse_server_config",
    "sanitize_server_name",
    "sanitize_tool_name",
]
