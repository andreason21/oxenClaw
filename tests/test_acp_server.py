"""Tests for the agent-side ACP server.

Two flavours:

  - **Unit**: drive `AcpServer.serve` over a `BytesIO` pair so we can
    assert exactly what bytes we wrote and read.
  - **Loopback E2E**: spawn `python -m oxenclaw.acp.server --backend
    fake` as a child and connect to it via our own
    `SubprocessAcpRuntime`. Round-trips the four foundational verbs
    over the real wire.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from oxenclaw.acp.fake_runtime import InMemoryFakeRuntime
from oxenclaw.acp.framing import BytesIOReader, BytesIOWriter, encode_message
from oxenclaw.acp.protocol import PROTOCOL_VERSION
from oxenclaw.acp.server import AcpServer
from oxenclaw.acp.subprocess_runtime import SubprocessAcpRuntime
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventStatus,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEvent,
)


def _wire_messages(buf: bytes) -> list[dict[str, Any]]:
    """Parse a buffer of NDJSON messages into a list of dicts."""
    out: list[dict[str, Any]] = []
    for line in buf.splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


# --- unit tests over BytesIO ----------------------------------------------


async def test_initialize_round_trip_unit() -> None:
    runtime = InMemoryFakeRuntime()
    server = AcpServer(runtime=runtime, agent_name="oxenclaw-test")

    inbound = io.BytesIO()
    inbound.write(
        encode_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientInfo": {"name": "test"},
                },
            }
        )
    )
    inbound.seek(0)
    outbound = io.BytesIO()

    reader = BytesIOReader(inbound)
    writer = BytesIOWriter(outbound)
    await server.serve(reader, writer)
    # Allow any spawned dispatch tasks to run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    msgs = _wire_messages(outbound.getvalue())
    assert len(msgs) == 1
    resp = msgs[0]
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert resp["result"]["agentInfo"]["name"] == "oxenclaw-test"


async def test_session_new_then_prompt_unit() -> None:
    runtime = InMemoryFakeRuntime()
    server = AcpServer(runtime=runtime)

    inbound = io.BytesIO()
    inbound.write(
        encode_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        )
    )
    inbound.write(
        encode_message(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "session/new",
                "params": {"_meta": {"sessionKey": "k1", "agent": "test"}},
            }
        )
    )
    # We can't know the sessionId until the response lands — for the
    # unit test, hard-code the predictable mint pattern. The server
    # mints "oxenclaw-0001", "oxenclaw-0002", etc. in arrival order.
    inbound.write(
        encode_message(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": "oxenclaw-0001",
                    "prompt": [{"type": "text", "text": "ping"}],
                },
            }
        )
    )
    inbound.seek(0)
    outbound = io.BytesIO()

    reader = BytesIOReader(inbound)
    writer = BytesIOWriter(outbound)
    await server.serve(reader, writer)
    # Yield enough times for all dispatch tasks to drain.
    for _ in range(10):
        await asyncio.sleep(0)

    msgs = _wire_messages(outbound.getvalue())
    # Expected wire trace: 3 responses + 1 session/update notification
    # interleaved with the prompt response.
    responses = [m for m in msgs if "id" in m]
    notifications = [m for m in msgs if "method" in m]

    by_id = {m["id"]: m for m in responses}
    assert by_id[1]["result"]["agentInfo"]["name"] == "oxenclaw"
    assert by_id[2]["result"]["sessionId"] == "oxenclaw-0001"
    assert by_id[3]["result"]["stopReason"] == "stop"

    # Fake echoes the prompt as one text-delta notification.
    upds = [
        n for n in notifications if n["method"] == "session/update"
    ]
    assert len(upds) >= 1
    body = upds[0]["params"]["update"]
    assert body["sessionUpdate"] == "agent_message_chunk"
    assert body["content"]["text"] == "ping"


async def test_unknown_method_returns_method_not_found_unit() -> None:
    runtime = InMemoryFakeRuntime()
    server = AcpServer(runtime=runtime)
    inbound = io.BytesIO()
    inbound.write(
        encode_message(
            {"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"}
        )
    )
    inbound.seek(0)
    outbound = io.BytesIO()
    await server.serve(BytesIOReader(inbound), BytesIOWriter(outbound))
    for _ in range(5):
        await asyncio.sleep(0)
    msgs = _wire_messages(outbound.getvalue())
    assert len(msgs) == 1
    err = msgs[0]
    assert err["id"] == 7
    assert err["error"]["code"] == -32601
    assert "unknown method" in err["error"]["message"]


async def test_invalid_initialize_params_returns_invalid_params() -> None:
    runtime = InMemoryFakeRuntime()
    server = AcpServer(runtime=runtime)
    inbound = io.BytesIO()
    # Missing protocolVersion → InitializeParams.model_validate raises.
    inbound.write(
        encode_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
    )
    inbound.seek(0)
    outbound = io.BytesIO()
    await server.serve(BytesIOReader(inbound), BytesIOWriter(outbound))
    for _ in range(5):
        await asyncio.sleep(0)
    msgs = _wire_messages(outbound.getvalue())
    assert msgs[0]["error"]["code"] == -32602


async def test_prompt_on_unknown_session_returns_invalid_params() -> None:
    runtime = InMemoryFakeRuntime()
    server = AcpServer(runtime=runtime)
    inbound = io.BytesIO()
    inbound.write(
        encode_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "session/prompt",
                "params": {
                    "sessionId": "never-opened",
                    "prompt": [{"type": "text", "text": "x"}],
                },
            }
        )
    )
    inbound.seek(0)
    outbound = io.BytesIO()
    await server.serve(BytesIOReader(inbound), BytesIOWriter(outbound))
    for _ in range(5):
        await asyncio.sleep(0)
    msgs = _wire_messages(outbound.getvalue())
    assert msgs[0]["error"]["code"] == -32602
    assert "unknown sessionId" in msgs[0]["error"]["message"]


# --- loopback E2E ---------------------------------------------------------


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


async def test_loopback_against_python_dash_m_oxenclaw_acp_server() -> None:
    """Spawn `python -m oxenclaw.acp.server --backend fake` and have
    our own SubprocessAcpRuntime client drive it over real stdio."""
    rt = SubprocessAcpRuntime(
        argv=[
            sys.executable,
            "-m",
            "oxenclaw.acp.server",
            "--backend",
            "fake",
        ],
        backend_id="loopback",
    )
    try:
        from oxenclaw.acp.manager import (
            AcpCloseSessionInput,
            AcpInitializeSessionInput,
            AcpRunTurnInput,
            AcpSessionManager,
        )
        from oxenclaw.acp.runtime_registry import AcpRuntimeBackend

        # Use a fresh manager to avoid clobbering any global state.
        mgr = AcpSessionManager()
        # Manually register the backend on our private manager via the
        # internal `_sessions` path: simplest is to bypass the registry
        # singleton and call ensure_session through the runtime directly.
        # But for proper e2e through the manager + registry, register it.
        from oxenclaw.acp import runtime_registry as registry_mod

        registry_mod.reset_for_tests()
        registry_mod.register_acp_runtime_backend(
            AcpRuntimeBackend(id="loopback", runtime=rt)
        )

        handle = await mgr.initialize_session(
            AcpInitializeSessionInput(
                session_key="loopback:1",
                agent="loopback-test",
                mode="oneshot",
                backend_id="loopback",
            )
        )
        assert handle.backend == "loopback"

        events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(
                    session_key="loopback:1",
                    text="hello loopback",
                    request_id="r1",
                )
            )
        )
        # The fake on the server emits one text_delta echoing the
        # input + done(stop). Our client wraps that as
        # AcpEventTextDelta + AcpEventDone(stop).
        assert any(
            isinstance(e, AcpEventTextDelta)
            and "hello loopback" in e.text
            for e in events
        )
        done = events[-1]
        assert isinstance(done, AcpEventDone)
        assert done.stop_reason == "stop"

        await mgr.close_session(
            AcpCloseSessionInput(session_key="loopback:1")
        )
        registry_mod.reset_for_tests()
    finally:
        await rt.aclose()


async def test_loopback_two_turns_share_one_session() -> None:
    """Same session, two prompts in a row, both succeed."""
    rt = SubprocessAcpRuntime(
        argv=[
            sys.executable,
            "-m",
            "oxenclaw.acp.server",
            "--backend",
            "fake",
        ],
        backend_id="loopback",
    )
    try:
        from oxenclaw.acp.manager import (
            AcpInitializeSessionInput,
            AcpRunTurnInput,
            AcpSessionManager,
        )
        from oxenclaw.acp.runtime_registry import AcpRuntimeBackend
        from oxenclaw.acp import runtime_registry as registry_mod

        registry_mod.reset_for_tests()
        registry_mod.register_acp_runtime_backend(
            AcpRuntimeBackend(id="loopback", runtime=rt)
        )
        mgr = AcpSessionManager()

        await mgr.initialize_session(
            AcpInitializeSessionInput(
                session_key="loopback:multi",
                agent="t",
                mode="persistent",
                backend_id="loopback",
            )
        )

        for prompt in ("first", "second"):
            events = await _drain(
                mgr.run_turn(
                    AcpRunTurnInput(
                        session_key="loopback:multi",
                        text=prompt,
                        request_id=f"r-{prompt}",
                    )
                )
            )
            assert any(
                isinstance(e, AcpEventTextDelta) and prompt in e.text
                for e in events
            )
            assert isinstance(events[-1], AcpEventDone)
            assert events[-1].stop_reason == "stop"
        registry_mod.reset_for_tests()
    finally:
        await rt.aclose()
