"""MCP (Model Context Protocol) client for sampyClaw.

Lets sampyClaw consume external MCP servers as if their tools were native
sampyClaw tools. Mirrors openclaw's `src/agents/mcp-*` + `pi-bundle-mcp-*`
modules. Server-side (exposing sampyClaw tools as an MCP server) is a
separate, future phase.

Public API:

- `MCPServerConfig`, `parse_server_config` — config parsing
- `MCPClient`, `MCPClientPool` — connection + JSON-RPC client
- `materialize_mcp_tools` — turn an MCPClientPool into sampyClaw `Tool`s
- `sanitize_server_name`, `build_safe_tool_name` — name shaping
"""

from sampyclaw.pi.mcp.adapter import materialize_mcp_tools
from sampyclaw.pi.mcp.client import MCPClient, MCPClientPool, MCPError
from sampyclaw.pi.mcp.config import (
    HttpServerConfig,
    MCPServerConfig,
    StdioServerConfig,
    parse_server_config,
)
from sampyclaw.pi.mcp.loader import build_pool_from_config, load_mcp_configs
from sampyclaw.pi.mcp.names import (
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
