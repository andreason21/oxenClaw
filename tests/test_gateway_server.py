"""Integration test for the WebSocket gateway: round-trip RPC + server-pushed event."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from websockets.asyncio.client import connect

from oxenclaw.gateway import (
    ChatSendParams,
    ChatSendResult,
    EventFrame,
    GatewayServer,
    Router,
)
from oxenclaw.gateway.protocol import ChatEvent


@pytest.fixture()
def router() -> Router:
    r = Router()

    @r.method("chat.send", ChatSendParams)
    async def _send(p: ChatSendParams) -> ChatSendResult:
        return ChatSendResult(message_id=f"{p.chat_id}:sent", timestamp=42.0)

    return r


async def _pick_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_roundtrip_and_event_push(router: Router) -> None:
    server = GatewayServer(router)
    port = await _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "dashboard",
                            "account_id": "main",
                            "chat_id": "99",
                            "text": "hi",
                        },
                    }
                )
            )
            raw_response = await asyncio.wait_for(ws.recv(), timeout=2.0)
            response = json.loads(raw_response)
            assert response["id"] == 1
            assert response["result"]["message_id"] == "99:sent"

            # Wait for the connection ctx to register, then broadcast an event.
            for _ in range(20):
                if server.connections:
                    break
                await asyncio.sleep(0.05)
            event = EventFrame(
                body=ChatEvent(
                    kind="chat",
                    agent_id="assistant",
                    session_key="main",
                    body={"text": "hello from server"},
                )
            )
            await server.broadcast(event)

            raw_event = await asyncio.wait_for(ws.recv(), timeout=2.0)
            frame = json.loads(raw_event)
            assert frame["type"] == "event"
            assert frame["body"]["kind"] == "chat"
            assert frame["body"]["body"]["text"] == "hello from server"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_request_shutdown_returns_serve_cleanly(router: Router) -> None:
    """`request_shutdown()` must make `serve()` return without cancellation."""
    server = GatewayServer(router, shutdown_drain_seconds=2.0)
    port = await _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        # Open a connection so there's something to drain.
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "dashboard",
                            "account_id": "main",
                            "chat_id": "1",
                            "text": "hi",
                        },
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
            server.request_shutdown()
            # The serve task should now finish cleanly.
            await asyncio.wait_for(task, timeout=5.0)
    except Exception:
        task.cancel()
        with contextlib.suppress(Exception):
            await task
        raise
    assert task.done()
    assert task.exception() is None


async def test_request_shutdown_idempotent(router: Router) -> None:
    server = GatewayServer(router, shutdown_drain_seconds=1.0)
    port = await _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        server.request_shutdown()
        server.request_shutdown()  # second call is a no-op
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
