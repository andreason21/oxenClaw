"""Agent-side ACP server — stdio JSON-RPC wrapper around an AcpRuntime.

This is the inverse direction of `SubprocessAcpRuntime`: instead of
oxenclaw spawning a child and acting as the ACP *client*, here
oxenclaw runs as a child process spawned by an ACP *client* (Zed,
another oxenclaw instance, or any conforming peer) and serves the
four foundational verbs.

The server is agnostic of which runtime backs it — pass any
`AcpRuntime` to `AcpServer`. The CLI entrypoint
(`python -m oxenclaw.acp.server`) defaults to the in-memory fake
so the loopback path works without external dependencies. PiAgent
integration arrives in a later commit as a dedicated `AcpRuntime`
implementation that wraps the agent loop.

Wire shape mirrors openclaw `src/acp/server.ts` at the level of the
four foundational verbs only — capability negotiation, mode/config
option round-trips, file/permission methods, and gateway bootstrap
are all out of scope for this commit.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.acp.framing import AcpFramingError, read_messages, write_message
from oxenclaw.acp.protocol import (
    PROTOCOL_VERSION,
    InitializeParams,
    NewSessionParams,
    PromptParams,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventStatus,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntime,
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimeTurnInput,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("acp.server")


# JSON-RPC standard error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class _ServerSession:
    session_id: str
    handle: AcpRuntimeHandle
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)


# The "sink" type for AcpServer.serve is anything framing.write_message
# accepts — either an asyncio.StreamWriter or any object with async
# `write(bytes) -> None`. We don't wrap or adapt; framing handles both.
Sink = Any


class AcpServer:
    """Drives a single inbound NDJSON stream, dispatching to a runtime."""

    def __init__(
        self,
        *,
        runtime: AcpRuntime,
        agent_name: str = "oxenclaw",
        agent_version: str = "0.0.1",
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        self._runtime = runtime
        self._agent_info = {"name": agent_name, "version": agent_version}
        self._protocol_version = protocol_version
        self._sessions: dict[str, _ServerSession] = {}
        self._session_counter = 0
        self._write_lock = asyncio.Lock()
        self._stopping = asyncio.Event()

    # ---- entrypoint -------------------------------------------------------

    async def serve(self, reader: Any, writer: Sink) -> None:
        in_flight: set[asyncio.Task[None]] = set()
        try:
            async for msg in read_messages(reader):
                if self._stopping.is_set():
                    break
                # Each request runs in its own task so a long
                # session/prompt does not block subsequent
                # session/cancel messages on the same wire.
                task = asyncio.create_task(self._dispatch(msg, writer))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
        except AcpFramingError as exc:
            logger.warning("acp.server framing error: %s", exc)
        except Exception:  # pragma: no cover — defensive
            logger.exception("acp.server reader crashed")
        finally:
            self._stopping.set()
            # Drain any tasks still running so unit tests can observe
            # the full output buffer before `serve()` returns.
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    def stop(self) -> None:
        self._stopping.set()

    # ---- dispatch --------------------------------------------------------

    async def _dispatch(self, msg: dict[str, Any], sink: Sink) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if not isinstance(method, str):
            if mid is not None:
                await self._send_error(
                    sink, mid, INVALID_REQUEST, "missing method"
                )
            return
        handler = self._handlers.get(method)
        if handler is None:
            if mid is not None:
                await self._send_error(
                    sink, mid, METHOD_NOT_FOUND, f"unknown method {method!r}"
                )
            return
        try:
            await handler(self, mid, params, sink)
        except _AcpServerHandlerError as exc:
            if mid is not None:
                await self._send_error(sink, mid, exc.code, exc.message)
        except Exception as exc:
            logger.exception("acp.server handler %s failed", method)
            if mid is not None:
                await self._send_error(
                    sink, mid, INTERNAL_ERROR, f"internal error: {exc}"
                )

    # ---- per-method handlers --------------------------------------------

    async def _handle_initialize(
        self, mid: int | str | None, params: dict[str, Any], sink: Sink
    ) -> None:
        if mid is None:
            return
        # Validate but ignore the client-advertised version; we always
        # respond with our own. Drift handling lives in a later commit.
        try:
            InitializeParams.model_validate(params)
        except Exception as exc:
            raise _AcpServerHandlerError(INVALID_PARAMS, str(exc)) from exc
        await self._send_result(
            sink,
            mid,
            {
                "protocolVersion": self._protocol_version,
                "agentInfo": self._agent_info,
            },
        )

    async def _handle_new_session(
        self, mid: int | str | None, params: dict[str, Any], sink: Sink
    ) -> None:
        if mid is None:
            return
        try:
            parsed = NewSessionParams.model_validate(params)
        except Exception as exc:
            raise _AcpServerHandlerError(INVALID_PARAMS, str(exc)) from exc
        meta = parsed.meta or {}
        session_key = (
            meta.get("sessionKey") if isinstance(meta, dict) else None
        ) or self._mint_session_key()
        agent_name = (
            meta.get("agent") if isinstance(meta, dict) else None
        ) or "default"
        mode = (meta.get("mode") if isinstance(meta, dict) else None) or "persistent"
        try:
            handle = await self._runtime.ensure_session(
                AcpRuntimeEnsureInput(
                    session_key=str(session_key),
                    agent=str(agent_name),
                    mode="persistent" if mode == "persistent" else "oneshot",
                    cwd=parsed.cwd,
                    resume_session_id=(
                        meta.get("resumeSessionId")
                        if isinstance(meta, dict)
                        else None
                    ),
                )
            )
        except Exception as exc:
            raise _AcpServerHandlerError(
                INTERNAL_ERROR, f"ensure_session failed: {exc}"
            ) from exc
        sid = self._mint_session_id()
        self._sessions[sid] = _ServerSession(session_id=sid, handle=handle)
        await self._send_result(sink, mid, {"sessionId": sid})

    async def _handle_prompt(
        self, mid: int | str | None, params: dict[str, Any], sink: Sink
    ) -> None:
        if mid is None:
            # session/prompt with no id is a protocol violation; ignore.
            return
        try:
            parsed = PromptParams.model_validate(params)
        except Exception as exc:
            raise _AcpServerHandlerError(INVALID_PARAMS, str(exc)) from exc
        sess = self._sessions.get(parsed.session_id)
        if sess is None:
            raise _AcpServerHandlerError(
                INVALID_PARAMS, f"unknown sessionId {parsed.session_id!r}"
            )
        # Concatenate text content blocks. Image/resource blocks are
        # ignored in this commit — translator gains support later.
        text = "".join(
            block.text
            for block in parsed.prompt
            if hasattr(block, "text")
        )
        sess.cancel_event.clear()
        stop_reason: str = "stop"
        try:
            async for ev in self._runtime.run_turn(
                AcpRuntimeTurnInput(
                    handle=sess.handle,
                    text=text,
                    mode="prompt",
                    request_id=str(mid),
                )
            ):
                update_payload = self._project_event(ev)
                if update_payload is not None:
                    await self._send_notification(
                        sink,
                        "session/update",
                        {
                            "sessionId": parsed.session_id,
                            "update": update_payload,
                        },
                    )
                if isinstance(ev, AcpEventDone):
                    stop_reason = ev.stop_reason or "stop"
                    break
                if isinstance(ev, AcpEventError):
                    raise _AcpServerHandlerError(
                        INTERNAL_ERROR, ev.message
                    )
                if sess.cancel_event.is_set():
                    stop_reason = "cancel"
                    break
        except _AcpServerHandlerError:
            raise
        except Exception as exc:
            raise _AcpServerHandlerError(
                INTERNAL_ERROR, f"run_turn failed: {exc}"
            ) from exc
        await self._send_result(sink, mid, {"stopReason": stop_reason})

    async def _handle_cancel(
        self, mid: int | str | None, params: dict[str, Any], sink: Sink
    ) -> None:
        # Cancel is a notification in some peers, a request in others.
        # Tolerate both. The spec is "no result" either way.
        sid = params.get("sessionId")
        if not isinstance(sid, str):
            if mid is not None:
                raise _AcpServerHandlerError(
                    INVALID_PARAMS, "sessionId required"
                )
            return
        sess = self._sessions.get(sid)
        if sess is None:
            if mid is not None:
                raise _AcpServerHandlerError(
                    INVALID_PARAMS, f"unknown sessionId {sid!r}"
                )
            return
        sess.cancel_event.set()
        with contextlib.suppress(Exception):
            await self._runtime.cancel(handle=sess.handle, reason="client_cancel")
        if mid is not None:
            await self._send_result(sink, mid, {})

    _handlers: dict[str, Callable[..., Awaitable[None]]] = {
        "initialize": _handle_initialize,
        "session/new": _handle_new_session,
        "session/prompt": _handle_prompt,
        "session/cancel": _handle_cancel,
    }

    # ---- helpers ---------------------------------------------------------

    def _mint_session_id(self) -> str:
        self._session_counter += 1
        return f"oxenclaw-{self._session_counter:04d}"

    def _mint_session_key(self) -> str:
        self._session_counter += 1
        return f"acp-server:{self._session_counter:04d}"

    @staticmethod
    def _project_event(ev: AcpRuntimeEvent) -> dict[str, Any] | None:
        if isinstance(ev, AcpEventTextDelta):
            tag = ev.tag or (
                "agent_thought_chunk"
                if ev.stream == "thought"
                else "agent_message_chunk"
            )
            return {
                "sessionUpdate": tag,
                "content": {"type": "text", "text": ev.text},
            }
        if isinstance(ev, AcpEventToolCall):
            return {
                "sessionUpdate": ev.tag or "tool_call_update",
                "toolCallId": ev.tool_call_id,
                "status": ev.status,
                "title": ev.title,
            }
        if isinstance(ev, AcpEventStatus):
            return {
                "sessionUpdate": ev.tag or "status",
                "text": ev.text,
            }
        if isinstance(ev, AcpEventError):
            # Errors short-circuit run_turn; no notification needed.
            return None
        if isinstance(ev, AcpEventDone):
            # Done is reflected in the session/prompt response, not
            # as a notification. Mirror openclaw — no `done` tag on
            # session/update.
            return None
        return None

    async def _send_result(
        self,
        sink: Sink,
        mid: int | str,
        result: dict[str, Any],
    ) -> None:
        async with self._write_lock:
            await write_message(
                sink,
                {"jsonrpc": "2.0", "id": mid, "result": result},
            )

    async def _send_error(
        self,
        sink: Sink,
        mid: int | str,
        code: int,
        message: str,
    ) -> None:
        async with self._write_lock:
            await write_message(
                sink,
                {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": code, "message": message},
                },
            )

    async def _send_notification(
        self,
        sink: Sink,
        method: str,
        params: dict[str, Any],
    ) -> None:
        async with self._write_lock:
            await write_message(
                sink,
                {"jsonrpc": "2.0", "method": method, "params": params},
            )


class _AcpServerHandlerError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# --- stdio adapters --------------------------------------------------------


async def _stdin_reader() -> asyncio.StreamReader:
    """Wrap sys.stdin as an asyncio StreamReader."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader


class _StdoutWriter:
    """Sync wrapper around sys.stdout.buffer for write_message."""

    async def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


# --- CLI entrypoint --------------------------------------------------------


def _build_runtime(name: str) -> AcpRuntime:
    if name == "fake":
        from oxenclaw.acp.fake_runtime import InMemoryFakeRuntime

        return InMemoryFakeRuntime()
    if name == "pi":
        from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime
        from oxenclaw.agents.factory import build_agent
        from oxenclaw.config import default_paths
        from oxenclaw.memory import (
            MemoryRetriever,
            build_embedder,
            memory_get_tool,
            memory_save_tool,
            memory_search_tool,
        )

        paths = default_paths()
        # Build a MemoryRetriever using the same default embedder the
        # gateway uses. Wrapped in try/except: if the operator hasn't
        # configured an embedder (no OLLAMA_HOST, no API key), the agent
        # still boots — without memory tools rather than crashing the
        # ACP server. Mirrors gateway_cmd.py's tolerance pattern.
        retriever: MemoryRetriever | None = None
        try:
            retriever = MemoryRetriever.for_root(
                paths,
                build_embedder("ollama"),  # type: ignore[arg-type]
            )
        except Exception:  # pragma: no cover — env-dependent
            retriever = None

        agent = build_agent(
            agent_id="pi",
            provider="ollama",
            memory=retriever,
        )
        # Register memory tools on the agent's tool registry — same
        # post-build wiring pattern gateway_cmd.py uses.
        if retriever is not None:
            agent._tools.register(memory_save_tool(retriever))
            agent._tools.register(memory_search_tool(retriever))
            agent._tools.register(memory_get_tool(retriever))
        # The primary ACP-client value: when the local PiAgent model
        # is the wrong tool for a hard sub-task, the model can call
        # `delegate_to_acp(runtime="claude", prompt=...)` to hand
        # that turn to a frontier ACP server. Imported lazily because
        # the pi backend is the path where this matters most.
        from oxenclaw.tools_pkg.acp_delegate_tool import acp_delegate_tool

        agent._tools.register(acp_delegate_tool())
        return PiAgentAcpRuntime(agent=agent)
    raise SystemExit(
        f"unknown backend {name!r} (built-in choices: 'fake', 'pi')"
    )


async def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oxenclaw acp",
        description="Run oxenclaw as an ACP agent over stdio.",
    )
    parser.add_argument(
        "--backend",
        default="fake",
        help="Runtime backend id (default: 'fake'). Real backends register at import time.",
    )
    args = parser.parse_args(argv)
    runtime = _build_runtime(args.backend)
    server = AcpServer(runtime=runtime)
    reader = await _stdin_reader()
    writer = _StdoutWriter()
    await server.serve(reader, writer)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run_cli(argv))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))


__all__ = ["AcpServer", "main"]
