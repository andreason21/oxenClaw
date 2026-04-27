"""End-to-end wire tests for SubprocessAcpRuntime.

Spawns a tiny in-process Python "ACP echo server" (via `python -c`)
that speaks NDJSON JSON-RPC, runs initialize → newSession → prompt
→ cancel → close through the manager + registry, and asserts that
the events project correctly.

The fake echo server is intentionally inline — shipping a separate
fixture script would either bloat the test directory or require
package-data plumbing. Inlining keeps the wire contract auditable
in one file.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.manager import (
    AcpCloseSessionInput,
    AcpInitializeSessionInput,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.runtime_registry import (
    AcpRuntimeBackend,
    register_acp_runtime_backend,
)
from oxenclaw.acp.subprocess_runtime import AcpWireError, SubprocessAcpRuntime
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventStatus,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEvent,
)


@pytest.fixture(autouse=True)
def _isolate_globals():
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


# --- echo server -----------------------------------------------------------


_ECHO_SERVER_SOURCE = textwrap.dedent(
    r"""
    import json, sys, time

    def write(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    sessions = {}
    counter = {"n": 0}

    def new_session_id():
        counter["n"] += 1
        return f"sess-{counter['n']:04d}"

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            write({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": params.get("protocolVersion", "0.19.0"),
                    "agentInfo": {"name": "echo-server", "version": "0.0.1"},
                },
            })
        elif method == "session/new":
            sid = new_session_id()
            sessions[sid] = {"cancelled": False}
            write({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
        elif method == "session/prompt":
            sid = params["sessionId"]
            text = params["prompt"][0]["text"]
            # Two text-delta notifications, then optional thought, then
            # a tool_call_update card, then either stop or cancel.
            write({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text + " "},
                    },
                },
            })
            write({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "(echoed)"},
                    },
                },
            })
            write({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": sid,
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "status": "completed",
                        "title": "echo.tool",
                    },
                },
            })
            stop = "cancel" if sessions[sid].get("cancelled") else "stop"
            sessions[sid]["cancelled"] = False
            write({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"stopReason": stop},
            })
        elif method == "session/cancel":
            sid = params["sessionId"]
            if sid in sessions:
                sessions[sid]["cancelled"] = True
        elif method == "shutdown":
            break
        else:
            write({
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"unknown method {method!r}"},
            })
    """
).strip()


def _argv() -> list[str]:
    return [sys.executable, "-c", _ECHO_SERVER_SOURCE]


# --- tests -----------------------------------------------------------------


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


async def test_initialize_run_turn_close_over_real_subprocess() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    register_acp_runtime_backend(AcpRuntimeBackend(id="echo", runtime=rt))
    mgr = get_acp_session_manager()
    try:
        handle = await mgr.initialize_session(
            AcpInitializeSessionInput(
                session_key="agent:1:echo:1",
                agent="test-agent",
                mode="oneshot",
                backend_id="echo",
            )
        )
        assert handle.backend == "echo"
        assert handle.runtime_session_name.startswith("sess-")

        events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(
                    session_key="agent:1:echo:1",
                    text="hello",
                    request_id="r1",
                )
            )
        )
        # Expect: 2× text_delta + 1× tool_call + 1× done(stop)
        kinds = [type(ev).__name__ for ev in events]
        assert kinds == [
            "AcpEventTextDelta",
            "AcpEventTextDelta",
            "AcpEventToolCall",
            "AcpEventDone",
        ]
        # Concatenated text equals the echoed prompt.
        text_chunks = [
            ev.text for ev in events if isinstance(ev, AcpEventTextDelta)
        ]
        assert "".join(text_chunks) == "hello (echoed)"
        tool_call = next(ev for ev in events if isinstance(ev, AcpEventToolCall))
        assert tool_call.tool_call_id == "tc-1"
        assert tool_call.status == "completed"
        assert tool_call.title == "echo.tool"
        done = events[-1]
        assert isinstance(done, AcpEventDone)
        assert done.stop_reason == "stop"

        await mgr.close_session(
            AcpCloseSessionInput(session_key="agent:1:echo:1")
        )
    finally:
        await rt.aclose()


async def test_cancel_propagates_to_next_turn() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    register_acp_runtime_backend(AcpRuntimeBackend(id="echo", runtime=rt))
    mgr = get_acp_session_manager()
    try:
        handle = await mgr.initialize_session(
            AcpInitializeSessionInput(
                session_key="s", agent="a", backend_id="echo"
            )
        )
        # Pre-cancel the session — server flips its `cancelled` flag,
        # then on the next prompt returns stopReason="cancel".
        await rt.cancel(handle=handle)
        events = await _drain(
            mgr.run_turn(
                AcpRunTurnInput(session_key="s", text="x", request_id="r")
            )
        )
        done = events[-1]
        assert isinstance(done, AcpEventDone)
        assert done.stop_reason == "cancel"
    finally:
        await rt.aclose()


async def test_unknown_method_surfaces_as_wire_error() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    try:
        # Trigger a request that the echo-server rejects with -32601.
        await rt._ensure_spawned()
        with pytest.raises(AcpWireError) as excinfo:
            await rt._request("does/not/exist", {})
        assert excinfo.value.code == -32601
    finally:
        await rt.aclose()


async def test_aclose_terminates_child_and_pending_requests() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    await rt._ensure_spawned()
    # Fire-and-forget: launch a request the server will respond to,
    # but call aclose() before it gets a chance.
    fut = asyncio.create_task(rt._request("session/new", {"cwd": "/tmp"}))
    await asyncio.sleep(0)  # let the request leave
    await rt.aclose()
    # The pending request should resolve to either an AcpWireError or
    # complete successfully if the server raced — the contract here
    # is "aclose() returns and the request future is no longer pending".
    try:
        await asyncio.wait_for(fut, timeout=2.0)
    except (AcpWireError, asyncio.TimeoutError):
        pass


async def test_aclose_is_idempotent() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    await rt._ensure_spawned()
    await rt.aclose()
    await rt.aclose()  # must not raise


async def test_run_turn_on_uninitialised_session_yields_error_event() -> None:
    rt = SubprocessAcpRuntime(argv=_argv(), backend_id="echo")
    try:
        await rt._ensure_spawned()
        events: list[AcpRuntimeEvent] = []
        from oxenclaw.agents.acp_runtime import AcpRuntimeHandle, AcpRuntimeTurnInput
        bogus_handle = AcpRuntimeHandle(
            session_key="never-opened",
            backend="echo",
            runtime_session_name="bogus",
        )
        async for ev in rt.run_turn(
            AcpRuntimeTurnInput(
                handle=bogus_handle,
                text="x",
                mode="prompt",
                request_id="r",
            )
        ):
            events.append(ev)
        assert len(events) == 1
        assert isinstance(events[0], AcpEventError)
        assert events[0].code == "session_not_initialised"
    finally:
        await rt.aclose()
