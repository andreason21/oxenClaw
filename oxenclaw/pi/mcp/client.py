"""MCP JSON-RPC 2.0 client.

Implements the small subset of the MCP protocol oxenClaw needs:

- `initialize` handshake
- `notifications/initialized` notification
- `tools/list` (with cursor-based pagination)
- `tools/call`

Higher-level concepts (catalog assembly, AgentTool adapters) live in
`adapter.py`. Tool name de-collision lives in `names.py`.

Concurrency model: each `MCPClient` owns one transport + one async task
that drains incoming messages and routes them to the right pending
request future. Concurrent `call_tool` invocations from the agent are
multiplexed safely.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from oxenclaw.pi.mcp.config import MCPServerConfig
from oxenclaw.pi.mcp.transport import (
    Transport,
    TransportClosed,
    open_transport,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.mcp.client")

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "oxenclaw-mcp"
CLIENT_VERSION = "0.1"


class MCPError(Exception):
    """An MCP server returned a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class MCPClient:
    """One MCP server connection."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._config = config
        self.server_name = config.server_name
        self._transport: Transport | None = transport
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False
        self._initialized = False
        self._server_capabilities: dict[str, Any] | None = None
        self._server_info: dict[str, Any] | None = None

    @property
    def description(self) -> str:
        return self._config.description

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def connect(self) -> None:
        if self._transport is None:
            self._transport = await open_transport(self._config)
        self._reader_task = asyncio.create_task(self._reader_loop())
        await asyncio.wait_for(
            self._initialize(),
            timeout=self._config.connection_timeout_seconds,
        )

    async def _initialize(self) -> None:
        result = await self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "version": CLIENT_VERSION,
                },
            },
        )
        if isinstance(result, dict):
            self._server_capabilities = (
                result.get("capabilities") if isinstance(result.get("capabilities"), dict) else None
            )
            self._server_info = (
                result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else None
            )
        await self._notify("notifications/initialized", {})
        self._initialized = True

    async def _reader_loop(self) -> None:
        assert self._transport is not None
        try:
            while not self._closed:
                try:
                    msg = await self._transport.receive()
                except TransportClosed as exc:
                    logger.debug(
                        "mcp:%s reader transport closed: %s",
                        self.server_name,
                        exc,
                    )
                    self._fail_all_pending(exc)
                    return
                self._dispatch(msg)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("mcp:%s reader crashed: %s", self.server_name, exc)
            self._fail_all_pending(exc)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict):
            return
        # Server-pushed notification — nothing to correlate.
        if "method" in message and "id" not in message:
            return
        msg_id = message.get("id")
        if not isinstance(msg_id, int):
            return
        future = self._pending.pop(msg_id, None)
        if future is None or future.done():
            return
        if "error" in message:
            err = message["error"]
            code = err.get("code", -1) if isinstance(err, dict) else -1
            text = err.get("message", "") if isinstance(err, dict) else str(err)
            data = err.get("data") if isinstance(err, dict) else None
            future.set_exception(MCPError(code, text, data))
            return
        future.set_result(message.get("result"))

    def _fail_all_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if self._closed or self._transport is None:
            raise TransportClosed(f"client '{self.server_name}' is closed")
        msg_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = future
        envelope: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            envelope["params"] = params
        try:
            await self._transport.send(envelope)
        except Exception:
            self._pending.pop(msg_id, None)
            raise
        try:
            if timeout is not None:
                return await asyncio.wait_for(future, timeout=timeout)
            return await future
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._closed or self._transport is None:
            raise TransportClosed(f"client '{self.server_name}' is closed")
        envelope: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            envelope["params"] = params
        await self._transport.send(envelope)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Walk all `tools/list` pages and concatenate."""
        if not self._initialized:
            raise RuntimeError(f"client '{self.server_name}' has not completed initialize")
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            page = await self._request("tools/list", params or None)
            if not isinstance(page, dict):
                break
            page_tools = page.get("tools")
            if isinstance(page_tools, list):
                tools.extend(t for t in page_tools if isinstance(t, dict))
            next_cursor = page.get("nextCursor")
            if isinstance(next_cursor, str) and next_cursor:
                cursor = next_cursor
                continue
            break
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Invoke `tools/call`. Returns the raw `CallToolResult` payload.

        Raises `MCPError` on JSON-RPC error. A returned dict with
        `isError=true` is *not* an exception — that's a tool-side error
        the model is expected to read.
        """
        if not self._initialized:
            raise RuntimeError(f"client '{self.server_name}' has not completed initialize")
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        if isinstance(result, dict):
            return result
        # Unexpected shape — wrap as a structured error so callers don't crash.
        return {"content": [{"type": "text", "text": str(result)}], "isError": True}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        if self._transport is not None:
            with suppress(Exception):
                await self._transport.close()
        self._transport = None
        self._reader_task = None


class MCPClientPool:
    """Manage a fixed set of MCPClients (one per configured server).

    Connection is lazy: clients are connected on first `get_clients()` call
    so that broken/missing servers don't block startup.
    """

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        log_level: int = logging.WARNING,
    ) -> None:
        self._configs = list(configs)
        self._log_level = log_level
        self._clients: dict[str, MCPClient] = {}
        self._failures: dict[str, str] = {}
        self._connect_lock = asyncio.Lock()
        self._connected = False

    @property
    def server_names(self) -> list[str]:
        return [c.server_name for c in self._configs]

    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failures)

    async def connect_all(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            for cfg in self._configs:
                client = MCPClient(cfg)
                try:
                    await client.connect()
                    self._clients[cfg.server_name] = client
                except Exception as exc:
                    logger.log(
                        self._log_level,
                        "mcp pool: failed to start '%s' (%s): %s",
                        cfg.server_name,
                        cfg.description,
                        exc,
                    )
                    self._failures[cfg.server_name] = str(exc)
                    with suppress(Exception):
                        await client.close()
            self._connected = True

    async def get_clients(self) -> dict[str, MCPClient]:
        await self.connect_all()
        return dict(self._clients)

    def get(self, server_name: str) -> MCPClient | None:
        return self._clients.get(server_name)

    async def close(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
        self._connected = False
