"""SubprocessAcpRuntime — first real `AcpRuntime` backend on the wire.

Spawns one child process per backend instance, talks NDJSON JSON-RPC
over its stdin/stdout, and routes responses + `session/update`
notifications through to the in-memory event stream that
`run_turn` exposes to the manager.

What this commit covers (minimum useful subset):

  - Spawn child via `asyncio.create_subprocess_exec`.
  - Reader task: NDJSON-decode every line of child stdout, classify
    into responses (correlated by JSON-RPC id) and notifications
    (forwarded to per-session queues).
  - Request/response correlation via `dict[id → asyncio.Future]`.
  - `ensure_session` → `initialize` (once per backend) + `session/new`.
  - `run_turn` → `session/prompt` + yield events from the per-session
    queue until the matching response arrives.
  - `cancel` → `session/cancel`.
  - `close` → close the session in the registry; the *backend
    process* lives until `aclose()` is called explicitly.

What we still skip (later commits):

  - capability negotiation (`InitializeResult.capabilities`)
  - mode / config option round-trips
  - the `_meta` overrides (sessionKey/sessionLabel/resetSession) — we
    just pass them through verbatim if the caller supplies them
  - resume via `session/load`
  - timeouts / lifetime caps on the wire (the parent-stream relay
    enforces operator-visible caps; protocol-level caps come later)

The backend is `asyncio`-only. One instance owns one child process;
register the same backend twice for two children. `aclose()` is
idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.acp.framing import AcpFramingError, read_messages, write_message
from oxenclaw.acp.protocol import (
    PROTOCOL_VERSION,
    CancelParams,
    InitializeParams,
    NewSessionParams,
    PromptContentText,
    PromptParams,
    notification_envelope,
    request_envelope,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventStatus,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimeTurnInput,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("acp.subprocess_runtime")


class AcpWireError(Exception):
    """Raised when the JSON-RPC peer returns an `error` response."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclass
class _SessionState:
    session_id: str
    queue: asyncio.Queue[AcpRuntimeEvent | None] = field(
        default_factory=asyncio.Queue
    )
    handle: AcpRuntimeHandle | None = None


class SubprocessAcpRuntime:
    """ACP backend that talks NDJSON to a child process over stdio."""

    backend_id_default: str = "subprocess"

    def __init__(
        self,
        *,
        argv: list[str],
        backend_id: str | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        client_info: dict[str, Any] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
    ) -> None:
        if not argv:
            raise ValueError("argv must be non-empty")
        self._argv = list(argv)
        self.backend_id = (backend_id or self.backend_id_default).strip().lower()
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._client_info = client_info or {"name": "oxenclaw"}
        self._protocol_version = protocol_version

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._sessions_by_key: dict[str, _SessionState] = {}
        self._sessions_by_id: dict[str, _SessionState] = {}
        self._initialised = asyncio.Event()
        self._closed = False
        self._spawn_lock = asyncio.Lock()

    # ---- AcpRuntime required surface --------------------------------------

    async def ensure_session(
        self, input: AcpRuntimeEnsureInput
    ) -> AcpRuntimeHandle:
        await self._ensure_spawned()
        existing = self._sessions_by_key.get(input.session_key)
        if existing is not None and existing.handle is not None:
            return existing.handle
        params = NewSessionParams(
            cwd=input.cwd,
            _meta={  # type: ignore[call-arg]
                "sessionKey": input.session_key,
                "agent": input.agent,
                "mode": input.mode,
                **(
                    {"resumeSessionId": input.resume_session_id}
                    if input.resume_session_id
                    else {}
                ),
            },
        )
        result = await self._request("session/new", params)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise AcpWireError(
                -32603, "session/new response missing sessionId", data=result
            )
        state = _SessionState(session_id=session_id)
        handle = AcpRuntimeHandle(
            session_key=input.session_key,
            backend=self.backend_id,
            runtime_session_name=session_id,
            cwd=input.cwd,
            backend_session_id=session_id,
            agent_session_id=input.resume_session_id,
        )
        state.handle = handle
        self._sessions_by_key[input.session_key] = state
        self._sessions_by_id[session_id] = state
        return handle

    def run_turn(
        self, input: AcpRuntimeTurnInput
    ) -> AsyncIterator[AcpRuntimeEvent]:
        return self._run_turn(input)

    async def _run_turn(
        self, input: AcpRuntimeTurnInput
    ) -> AsyncIterator[AcpRuntimeEvent]:
        state = self._sessions_by_key.get(input.handle.session_key)
        if state is None:
            yield AcpEventError(
                message=f"session {input.handle.session_key!r} not initialised",
                code="session_not_initialised",
            )
            return
        params = PromptParams(
            sessionId=state.session_id,  # type: ignore[call-arg]
            prompt=[PromptContentText(text=input.text)],
        )
        # Drain any stale events from the queue before this turn.
        self._drain_queue(state.queue)
        # Fire the request without awaiting — events arrive as
        # `session/update` notifications while we wait, and the
        # final stop reason comes back as the response.
        req_id = self._allocate_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send_envelope(
                request_envelope(id=req_id, method="session/prompt", params=params)
            )
        except Exception as exc:
            self._pending.pop(req_id, None)
            yield AcpEventError(message=str(exc), code="wire_send_failed")
            return
        # Concurrently consume notifications from the queue and wait
        # for the prompt response. `asyncio.wait` accepts a Future
        # directly — no need to wrap it in a Task.
        try:
            while True:
                queue_get = asyncio.create_task(state.queue.get())
                done, _pending = await asyncio.wait(
                    {queue_get, future},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_get in done:
                    ev = queue_get.result()
                    if ev is None:
                        # Sentinel — child closed mid-turn.
                        yield AcpEventError(
                            message="child closed before stopReason arrived",
                            code="child_closed",
                        )
                        return
                    yield ev
                    if future.done():
                        # Drain any remaining queued events that
                        # arrived before the response. This keeps
                        # ordering: notifications first, then done.
                        while True:
                            try:
                                ev = state.queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            if ev is None:
                                continue
                            yield ev
                        break
                else:
                    # response arrived first — cancel the queue read
                    # and drain any queued events before yielding done.
                    queue_get.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await queue_get
                    while True:
                        try:
                            ev = state.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if ev is None:
                            continue
                        yield ev
                    break
            # Final result handling.
            try:
                result = future.result()
            except AcpWireError as wire_err:
                yield AcpEventError(
                    message=wire_err.message,
                    code=str(wire_err.code),
                )
                return
            stop_reason = (
                result.get("stopReason")
                if isinstance(result, dict)
                else None
            )
            yield AcpEventDone(stop_reason=stop_reason or "stop")
        finally:
            self._pending.pop(req_id, None)

    async def cancel(
        self, *, handle: AcpRuntimeHandle, reason: str | None = None
    ) -> None:
        state = self._sessions_by_key.get(handle.session_key)
        if state is None:
            return
        await self._notify(
            "session/cancel",
            CancelParams(sessionId=state.session_id),  # type: ignore[call-arg]
        )

    async def close(
        self,
        *,
        handle: AcpRuntimeHandle,
        reason: str,
        discard_persistent_state: bool = False,
    ) -> None:
        state = self._sessions_by_key.pop(handle.session_key, None)
        if state is None:
            return
        self._sessions_by_id.pop(state.session_id, None)
        # Drop any stale events; signal any in-flight run_turn.
        await state.queue.put(None)

    # ---- backend lifecycle ------------------------------------------------

    async def aclose(self) -> None:
        """Tear down the child process and any in-flight tasks.

        Idempotent. Operators or tests should call this when done;
        the registry has no notion of backend lifetime.
        """
        if self._closed:
            return
        self._closed = True
        # Resolve any pending requests with a closed-error.
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    AcpWireError(-32001, "backend closed before response")
                )
        self._pending.clear()
        # Release any in-flight run_turn iterators.
        for state in self._sessions_by_key.values():
            await state.queue.put(None)
        # Stop reader/stderr tasks.
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        # Terminate the process.
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass

    # ---- internals --------------------------------------------------------

    async def _ensure_spawned(self) -> None:
        if self._closed:
            raise AcpWireError(-32001, "backend is closed")
        if self._proc is not None and self._proc.returncode is None:
            return
        async with self._spawn_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            env = os.environ.copy()
            if self._env:
                env.update(self._env)
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            await self._handshake()

    async def _handshake(self) -> None:
        params = InitializeParams(
            protocolVersion=self._protocol_version,  # type: ignore[call-arg]
            clientInfo=self._client_info,  # type: ignore[call-arg]
        )
        await self._request("initialize", params)
        self._initialised.set()

    async def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for msg in read_messages(self._proc.stdout):
                self._dispatch(msg)
        except AcpFramingError as exc:
            logger.warning("acp.subprocess framing error: %s", exc)
        except Exception:  # pragma: no cover — defensive
            logger.exception("acp.subprocess reader crashed")
        finally:
            # Notify any waiters that the child closed.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(
                        AcpWireError(-32001, "child stdout closed")
                    )
            for state in list(self._sessions_by_key.values()):
                with contextlib.suppress(Exception):
                    state.queue.put_nowait(None)

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                # ACP convention: stderr is human-readable diagnostics.
                logger.info(
                    "acp.subprocess[%s] %s",
                    self.backend_id,
                    line.decode("utf-8", errors="replace").rstrip(),
                )
        except asyncio.CancelledError:
            return

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            # Response.
            mid = msg.get("id")
            if not isinstance(mid, int):
                logger.warning("acp.subprocess: response with non-int id %r", mid)
                return
            future = self._pending.pop(mid, None)
            if future is None or future.done():
                return
            err = msg.get("error")
            if err:
                future.set_exception(
                    AcpWireError(
                        int(err.get("code", -32603)),
                        str(err.get("message", "wire error")),
                        data=err.get("data"),
                    )
                )
                return
            future.set_result(msg.get("result"))
            return
        method = msg.get("method")
        if not method:
            logger.warning("acp.subprocess: unhandled message %r", msg)
            return
        if method == "session/update":
            self._handle_session_update(msg.get("params") or {})
            return
        # Unknown notification — log and drop. Client-initiated
        # requests from the agent (file/permission/terminal) aren't
        # supported in this commit.
        logger.info(
            "acp.subprocess: unhandled notification method=%s", method
        )

    def _handle_session_update(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        update = params.get("update") or {}
        if not isinstance(session_id, str) or not isinstance(update, dict):
            return
        state = self._sessions_by_id.get(session_id)
        if state is None:
            return
        event = self._project_update(update)
        if event is None:
            return
        state.queue.put_nowait(event)

    def _project_update(self, update: dict[str, Any]) -> AcpRuntimeEvent | None:
        """Map an ACP `session/update` payload to an AcpRuntimeEvent.

        Minimum-useful projection — covers text deltas, thoughts,
        tool-call cards, and a generic status fallback. Plan
        events / usage updates / mode changes are forwarded as
        AcpEventStatus with the original tag so consumers can
        filter without losing the signal.
        """
        tag = update.get("sessionUpdate")
        if not isinstance(tag, str):
            return None
        if tag == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                return AcpEventTextDelta(text=text, stream="output", tag=tag)
            return None
        if tag == "agent_thought_chunk":
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else None
            if isinstance(text, str):
                return AcpEventTextDelta(text=text, stream="thought", tag=tag)
            return None
        if tag in ("tool_call", "tool_call_update"):
            return AcpEventToolCall(
                text=str(update.get("title") or update.get("text") or ""),
                tag=tag,
                tool_call_id=update.get("toolCallId"),
                status=update.get("status"),
                title=update.get("title"),
            )
        # Fallback: status with the original tag preserved.
        text = (
            update.get("text")
            or update.get("summary")
            or update.get("title")
            or ""
        )
        return AcpEventStatus(text=str(text), tag=tag)

    @staticmethod
    def _drain_queue(q: asyncio.Queue[Any]) -> None:
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _allocate_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def _request(self, method: str, params: Any) -> Any:
        req_id = self._allocate_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            await self._send_envelope(
                request_envelope(id=req_id, method=method, params=params)
            )
        except Exception:
            self._pending.pop(req_id, None)
            raise
        return await future

    async def _notify(self, method: str, params: Any) -> None:
        await self._send_envelope(
            notification_envelope(method=method, params=params)
        )

    async def _send_envelope(self, envelope: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AcpWireError(-32001, "child stdin not available")
        await write_message(self._proc.stdin, envelope)


__all__ = ["AcpWireError", "SubprocessAcpRuntime"]
