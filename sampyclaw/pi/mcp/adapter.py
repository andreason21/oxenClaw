"""Bridge MCP tools into sampyClaw's `ToolRegistry`.

`materialize_mcp_tools(pool, reserved=...)` connects all clients in the
pool, walks `tools/list` per server, and returns a list of `Tool` objects
ready to feed `ToolRegistry.register_all(...)`.

Tool name shape: `<safe_server>__<safe_tool>`, capped at 64 chars,
disambiguated against `reserved` names (typically the names of native
sampyClaw tools the agent already has).
"""

from __future__ import annotations

import json
from typing import Any

from sampyclaw.agents.tools import Tool
from sampyclaw.pi.mcp.client import MCPClientPool, MCPError
from sampyclaw.pi.mcp.names import (
    build_safe_tool_name,
    normalize_reserved_names,
    sanitize_server_name,
)

_DEFAULT_TOOL_TIMEOUT_SECONDS = 60.0


def _flatten_call_result(server: str, tool: str, result: dict[str, Any]) -> str:
    """Render a `CallToolResult` as a string the agent can read.

    MCP returns `content` as a list of typed blocks (text/image/...) and
    optional `structuredContent`. We:
      - Concatenate all `text` blocks.
      - Append a JSON dump of `structuredContent` if present and there's
        no text body.
      - For non-text blocks, we emit a one-line marker so the model knows
        a non-text result was returned (it can then ask for the structured
        form via a follow-up tool if the server supports one).
    """
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif kind == "image":
                mime = block.get("mimeType", "image")
                parts.append(f"[{mime} image returned by {server}__{tool}]")
            elif kind == "resource":
                resource = block.get("resource") or {}
                uri = resource.get("uri") if isinstance(resource, dict) else None
                parts.append(f"[resource: {uri or '<unknown>'} from {server}__{tool}]")
            else:
                parts.append(f"[{kind or 'unknown'} block from {server}__{tool}]")
    if not parts:
        structured = result.get("structuredContent")
        if structured is not None:
            parts.append(json.dumps(structured, ensure_ascii=False, indent=2))
        else:
            parts.append(
                json.dumps(
                    {
                        "status": "error" if result.get("isError") else "ok",
                        "server": server,
                        "tool": tool,
                    },
                    ensure_ascii=False,
                )
            )
    if result.get("isError") is True:
        parts.append("\n[mcp tool reported isError=true]")
    return "\n".join(parts)


class _MCPProxyTool:
    """Implements the sampyClaw `Tool` Protocol for one MCP tool."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        pool: MCPClientPool,
        server_name: str,
        original_tool_name: str,
        timeout_seconds: float,
    ) -> None:
        self.name = name
        self.description = description
        self._input_schema = input_schema
        self._pool = pool
        self._server_name = server_name
        self._original_tool_name = original_tool_name
        self._timeout_seconds = timeout_seconds

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_schema

    async def execute(self, args: dict[str, Any]) -> str:
        client = self._pool.get(self._server_name)
        if client is None:
            failures = self._pool.failures.get(self._server_name)
            return f"mcp tool unavailable: server '{self._server_name}' is not connected" + (
                f" ({failures})" if failures else ""
            )
        try:
            result = await client.call_tool(
                self._original_tool_name,
                args,
                timeout=self._timeout_seconds,
            )
        except MCPError as exc:
            return f"mcp error from {self._server_name}: {exc.message}"
        return _flatten_call_result(self._server_name, self._original_tool_name, result)


def _coerce_input_schema(raw: Any) -> dict[str, Any]:
    """MCP tools must declare a JSON Schema for inputs. Default to an empty
    object schema if the server returned something we can't use — most
    OpenAI-compatible providers reject `null` schemas."""
    if isinstance(raw, dict) and raw.get("type") in ("object", None):
        # Even when type is omitted, treat it as an object schema so
        # downstream serializers don't complain.
        if "type" not in raw:
            raw = {**raw, "type": "object"}
        return raw
    return {"type": "object", "properties": {}, "additionalProperties": True}


async def materialize_mcp_tools(
    pool: MCPClientPool,
    *,
    reserved_names: list[str] | tuple[str, ...] | None = None,
    timeout_seconds: float = _DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> list[Tool]:
    """Walk every connected MCP server in `pool` and return adapter tools.

    The pool is connected lazily. Servers that fail to start are skipped
    silently — their failure reason is available via `pool.failures`.
    """
    clients = await pool.get_clients()
    reserved = normalize_reserved_names(reserved_names)
    used_server_names: set[str] = set()
    flat_tools: list[tuple[str, str, str, str, dict[str, Any], dict[str, Any]]] = []

    for server_name, client in clients.items():
        safe_server = sanitize_server_name(server_name, used_server_names)
        try:
            listed = await client.list_tools()
        except Exception:
            continue
        for tool in listed:
            tname = tool.get("name")
            if not isinstance(tname, str) or not tname.strip():
                continue
            description = (
                tool.get("description") if isinstance(tool.get("description"), str) else None
            )
            title = tool.get("title") if isinstance(tool.get("title"), str) else None
            schema = _coerce_input_schema(tool.get("inputSchema"))
            fallback = f"Provided by MCP server '{server_name}' ({client.description})."
            flat_tools.append(
                (
                    server_name,
                    safe_server,
                    tname.strip(),
                    description or title or fallback,
                    schema,
                    tool,
                )
            )

    flat_tools.sort(key=lambda t: (t[1], t[2], t[0]))

    out: list[Tool] = []
    for (
        server_name,
        safe_server,
        original_tool,
        desc,
        schema,
        _raw,
    ) in flat_tools:
        safe_name = build_safe_tool_name(
            server_name=safe_server,
            tool_name=original_tool,
            reserved_names=reserved,
        )
        reserved.add(safe_name.lower())
        out.append(
            _MCPProxyTool(
                name=safe_name,
                description=desc,
                input_schema=schema,
                pool=pool,
                server_name=server_name,
                original_tool_name=original_tool,
                timeout_seconds=timeout_seconds,
            )
        )
    out.sort(key=lambda t: t.name)
    return out
