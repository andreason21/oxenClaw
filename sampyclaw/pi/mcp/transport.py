"""Transport layer for MCP — stdio (subprocess) + HTTP+SSE.

Each transport exposes:
- `async send(message: dict) -> None`
- `async receive() -> dict`  (returns one JSON-RPC message)
- `async close() -> None`

Higher-level framing (id assignment, request/response correlation) lives
in `client.py`. This module is intentionally protocol-agnostic.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from typing import Any, Protocol

from sampyclaw.pi.mcp.config import (
    HttpServerConfig,
    MCPServerConfig,
    StdioServerConfig,
)
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.mcp.transport")


class TransportClosed(Exception):
    """Raised when send/receive is attempted after close."""


class Transport(Protocol):
    server_name: str

    async def send(self, message: dict[str, Any]) -> None: ...
    async def receive(self) -> dict[str, Any]: ...
    async def close(self) -> None: ...


class StdioTransport:
    """JSON-RPC over a child process's stdin/stdout (line-delimited JSON)."""

    def __init__(self, config: StdioServerConfig) -> None:
        self._config = config
        self.server_name = config.server_name
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        env = os.environ.copy()
        # Strip dangerous keys from inherited env (don't propagate the
        # gateway's own loader-affecting vars). Even keys the user explicitly
        # listed in their MCP config that match the dangerous list have
        # already been dropped by `parse_server_config`.
        for key in (
            "LD_PRELOAD",
            "LD_AUDIT",
            "DYLD_INSERT_LIBRARIES",
            "PYTHONSTARTUP",
        ):
            env.pop(key, None)
        if self._config.env:
            env.update(self._config.env)
        self._proc = await asyncio.create_subprocess_exec(
            self._config.command,
            *self._config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._config.cwd,
            env=env,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                try:
                    msg = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                if msg:
                    logger.debug("mcp:%s stderr: %s", self.server_name, msg)
        except asyncio.CancelledError:
            return

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise TransportClosed(
                f"stdio transport for '{self.server_name}' is closed"
            )
        payload = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            self._proc.stdin.write(payload.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportClosed(
                f"stdio transport for '{self.server_name}' broke: {exc}"
            ) from exc

    async def receive(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise TransportClosed(
                f"stdio transport for '{self.server_name}' is closed"
            )
        line = await self._proc.stdout.readline()
        if not line:
            raise TransportClosed(
                f"stdio transport for '{self.server_name}' closed by peer"
            )
        try:
            decoded = line.decode("utf-8")
            return json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise TransportClosed(
                f"stdio transport for '{self.server_name}': non-JSON line: {exc}"
            ) from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            with suppress(ProcessLookupError):
                if proc.stdin is not None:
                    with suppress(Exception):
                        proc.stdin.close()
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            proc.kill()
                            await proc.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        self._proc = None
        self._stderr_task = None


class HttpSseTransport:
    """JSON-RPC over HTTP + SSE response stream.

    The MCP HTTP transport is a long-lived SSE GET that delivers
    server-pushed JSON-RPC messages, plus client-driven JSON POSTs that
    return their result via the SSE channel (correlated by `id`).

    For the streamable-http variant, each POST returns the response on the
    same connection — we treat both flavors the same at this layer (any
    inbound JSON-RPC message goes onto the receive queue).
    """

    def __init__(self, config: HttpServerConfig) -> None:
        from sampyclaw.security.net.guarded_fetch import guarded_session
        from sampyclaw.security.net.policy import policy_from_env

        self._config = config
        self.server_name = config.server_name
        self._policy = policy_from_env()
        self._session_cm = guarded_session(self._policy)
        self._session = None  # type: ignore[var-annotated]
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._sse_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        from sampyclaw.security.net.guarded_fetch import policy_pre_flight

        policy_pre_flight(self._config.url, self._policy)
        self._session = await self._session_cm.__aenter__()  # type: ignore[attr-defined]
        # Open SSE stream.
        self._sse_task = asyncio.create_task(self._read_sse_loop())

    async def _read_sse_loop(self) -> None:
        assert self._session is not None
        headers = {"Accept": "text/event-stream"}
        if self._config.headers:
            headers.update(self._config.headers)
        try:
            async with self._session.get(
                self._config.url, headers=headers
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        "mcp:%s SSE GET failed: %s %s",
                        self.server_name,
                        resp.status,
                        body[:200],
                    )
                    await self._inbox.put(
                        {
                            "_transport_error": True,
                            "status": resp.status,
                            "reason": body[:500],
                        }
                    )
                    return
                buffer: list[str] = []
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8", errors="replace").rstrip(
                        "\r\n"
                    )
                    if line == "":
                        if buffer:
                            await self._dispatch_sse_event(buffer)
                            buffer = []
                        continue
                    buffer.append(line)
                if buffer:
                    await self._dispatch_sse_event(buffer)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "mcp:%s SSE loop crashed: %s", self.server_name, exc
            )
            with suppress(asyncio.QueueFull):
                await self._inbox.put(
                    {"_transport_error": True, "reason": str(exc)}
                )

    async def _dispatch_sse_event(self, lines: list[str]) -> None:
        data_parts: list[str] = []
        event_type = "message"
        for line in lines:
            if line.startswith("data:"):
                data_parts.append(line[len("data:") :].lstrip())
            elif line.startswith("event:"):
                event_type = line[len("event:") :].strip()
        if not data_parts or event_type != "message":
            return
        data = "\n".join(data_parts)
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            logger.debug(
                "mcp:%s sse data not json: %s (%r)",
                self.server_name,
                exc,
                data[:200],
            )
            return
        if isinstance(parsed, dict):
            await self._inbox.put(parsed)

    async def send(self, message: dict[str, Any]) -> None:
        if self._closed or self._session is None:
            raise TransportClosed(
                f"http transport for '{self.server_name}' is closed"
            )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._config.headers:
            headers.update(self._config.headers)
        async with self._session.post(
            self._config.url, json=message, headers=headers
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise TransportClosed(
                    f"http POST {resp.status}: {body[:500]}"
                )
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                payload = await resp.json()
                if isinstance(payload, dict):
                    await self._inbox.put(payload)

    async def receive(self) -> dict[str, Any]:
        if self._closed and self._inbox.empty():
            raise TransportClosed(
                f"http transport for '{self.server_name}' is closed"
            )
        msg = await self._inbox.get()
        if msg.get("_transport_error"):
            raise TransportClosed(
                f"http transport for '{self.server_name}': "
                f"{msg.get('reason') or 'transport error'}"
            )
        return msg

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sse_task is not None:
            self._sse_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._sse_task
        if self._session is not None:
            with suppress(Exception):
                await self._session_cm.__aexit__(None, None, None)  # type: ignore[attr-defined]
        self._session = None
        self._sse_task = None


async def open_transport(config: MCPServerConfig) -> Transport:
    """Construct + start a transport for a parsed config."""
    if isinstance(config, StdioServerConfig):
        t = StdioTransport(config)
        await t.start()
        return t
    transport = HttpSseTransport(config)
    await transport.start()
    return transport
