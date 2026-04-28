"""PiAgentAcpRuntime — wrap a PiAgent so an ACP client drives a real turn.

This is the adapter that finally puts the live agent loop behind the
ACP wire. Replaces the `fake` backend in `oxenclaw acp --backend pi`
with PiAgent's actual `handle()` flow, which means an external ACP
client (Zed, another oxenclaw, etc.) gets:

  - real LLM streaming text via `agent_message_chunk` notifications
  - PiAgent's full tool stack (read/write/edit/grep/glob/shell/process,
    update_plan, memory_save/search, skill_resolver, …)
  - ConversationHistory persistence on the agent side, exactly like
    a chat.send turn — so dashboard chat-history reads still work
    while the ACP client is connected
  - `stopReason=cancel` propagation when the client sends
    `session/cancel`

Tool-call telemetry is now projected mid-flight: a per-turn
before_tool_use / after_tool_use hook pair is registered on the
agent's HookRunner and pushes `tool_call` (pending) and
`tool_call_update` (completed/failed) events into a queue that the
run-turn loop drains between text yields. Hooks are filtered by
`session_key` so concurrent ACP sessions on the same agent don't
cross-pollute each other's telemetry. The hooks are uninstalled in
a finally block — even on cancellation, error, or generator close.

What this commit deliberately doesn't ship:

  - image / resource content blocks. We ignore non-text blocks for
    now; PiAgent's multimodal pipeline already handles them when
    InboundEnvelope.media is populated, so the wiring is mechanical.
  - capability negotiation (`InitializeResult.capabilities`). The
    server still returns the basic agentInfo only.

The adapter holds no PiAgent reference internally — it's passed via
the constructor so the same runtime can wrap different agents
(per-process, per-agent_id) if a future deployment registers
multiple PiAgent backends with different model_ids.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimeTurnInput,
)
from oxenclaw.agents.base import AgentContext
from oxenclaw.pi.hooks import (
    AfterToolUseHook,
    BeforeToolUseHook,
    HookContext,
    HookRunner,
)
from oxenclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
)
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("acp.pi_agent_runtime")

# ACP doesn't have a "channel" the way our InboundEnvelope expects. We
# synthesise one so the agent's persistence + dispatch paths see a
# consistent name across turns. Distinct from "dashboard" so operators
# can tell ACP-driven sessions apart in chat-history files.
_ACP_CHANNEL = "acp"


@dataclass
class _PiSession:
    handle: AcpRuntimeHandle
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False


@dataclass
class _ToolTelemetry:
    """Per-turn capture buffer for before/after_tool_use hook taps."""

    session_key: str
    queue: asyncio.Queue[AcpEventToolCall] = field(default_factory=asyncio.Queue)
    counter: int = 0
    # FIFO of tool_call_ids minted in before_tool_use, popped in
    # after_tool_use. Tools execute sequentially per session so a
    # plain list is enough.
    pending_ids: list[str] = field(default_factory=list)

    def make_before_hook(self) -> BeforeToolUseHook:
        async def _before(
            tool_name: str,
            args: dict[str, Any],
            ctx: HookContext,
        ) -> None:
            if ctx.session_key != self.session_key:
                return None
            self.counter += 1
            tid = f"acp-tc-{self.counter}"
            self.pending_ids.append(tid)
            self.queue.put_nowait(
                AcpEventToolCall(
                    text=tool_name,
                    tag="tool_call",
                    tool_call_id=tid,
                    status="pending",
                    title=tool_name,
                )
            )
            return None

        return _before

    def make_after_hook(self) -> AfterToolUseHook:
        async def _after(
            tool_name: str,
            args: dict[str, Any],
            output: str,
            is_error: bool,
            ctx: HookContext,
        ) -> None:
            if ctx.session_key != self.session_key:
                return
            tid = self.pending_ids.pop(0) if self.pending_ids else (f"acp-tc-orphan-{self.counter}")
            self.queue.put_nowait(
                AcpEventToolCall(
                    text=tool_name,
                    tag="tool_call_update",
                    tool_call_id=tid,
                    status="failed" if is_error else "completed",
                    title=tool_name,
                )
            )

        return _after

    def drain(self) -> list[AcpEventToolCall]:
        out: list[AcpEventToolCall] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out


class PiAgentAcpRuntime:
    """AcpRuntime backed by a real PiAgent instance.

    One adapter wraps one agent. The session_key passed via
    `ensure_session` is forwarded verbatim to PiAgent's own
    SessionManager via `AgentContext.session_key`, so dashboard-side
    chat.history reads and ACP-side prompts share storage.
    """

    backend_id_default: str = "pi"

    def __init__(
        self,
        *,
        agent: Any,
        backend_id: str | None = None,
        sender_id: str = "acp-client",
    ) -> None:
        self._agent = agent
        self.backend_id = (backend_id or self.backend_id_default).strip().lower()
        self._sender_id = sender_id
        self._sessions: dict[str, _PiSession] = {}

    # ---- AcpRuntime required surface -------------------------------------

    async def ensure_session(self, input: AcpRuntimeEnsureInput) -> AcpRuntimeHandle:
        existing = self._sessions.get(input.session_key)
        if existing is not None and not existing.closed:
            return existing.handle
        handle = AcpRuntimeHandle(
            session_key=input.session_key,
            backend=self.backend_id,
            runtime_session_name=input.session_key,
            cwd=input.cwd,
            agent_session_id=input.resume_session_id,
        )
        self._sessions[input.session_key] = _PiSession(handle=handle)
        return handle

    def run_turn(self, input: AcpRuntimeTurnInput) -> AsyncIterator[AcpRuntimeEvent]:
        return self._run_turn(input)

    async def _run_turn(self, input: AcpRuntimeTurnInput) -> AsyncIterator[AcpRuntimeEvent]:
        state = self._sessions.get(input.handle.session_key)
        if state is None or state.closed:
            yield AcpEventError(
                message=f"session {input.handle.session_key!r} not initialised",
                code="session_not_initialised",
            )
            return
        # Pre-cancel: if cancel was set before run_turn started, honour
        # it immediately and clear the flag so the next turn starts
        # fresh. Matches InMemoryFakeRuntime's contract.
        if state.cancelled.is_set():
            state.cancelled.clear()
            yield AcpEventDone(stop_reason="cancel")
            return
        envelope = InboundEnvelope(
            channel=_ACP_CHANNEL,
            account_id="main",
            target=ChannelTarget(
                channel=_ACP_CHANNEL,
                account_id="main",
                chat_id=input.handle.session_key,
            ),
            sender_id=self._sender_id,
            text=input.text or "",
            received_at=time.time(),
        )
        ctx = AgentContext(
            agent_id=getattr(self._agent, "id", "pi"),
            session_key=input.handle.session_key,
        )
        telemetry = _ToolTelemetry(session_key=input.handle.session_key)
        uninstall = self._install_tool_hooks(telemetry)
        try:
            try:
                async for send_params in self._agent.handle(envelope, ctx):
                    # Drain any tool events that fired since the last
                    # yield. This puts tool_call cards before the text
                    # delta they preceded, which is the natural order
                    # an operator expects ("we called X, here's the
                    # answer").
                    for tool_ev in telemetry.drain():
                        yield tool_ev
                    if state.cancelled.is_set():
                        state.cancelled.clear()
                        yield AcpEventDone(stop_reason="cancel")
                        return
                    text = (send_params.text or "").strip("\n")
                    if not text:
                        continue
                    yield AcpEventTextDelta(
                        text=send_params.text or "",
                        stream="output",
                        tag="agent_message_chunk",
                    )
            except asyncio.CancelledError:
                state.cancelled.clear()
                yield AcpEventDone(stop_reason="cancel")
                return
            except Exception as exc:
                logger.exception("PiAgentAcpRuntime: handle() failed")
                yield AcpEventError(
                    message=f"agent handle failed: {exc}",
                    code="agent_handle_failed",
                )
                return
            # Final drain — catch tool events that fired between the
            # last text yield and the end of the turn.
            for tool_ev in telemetry.drain():
                yield tool_ev
            if state.cancelled.is_set():
                state.cancelled.clear()
                yield AcpEventDone(stop_reason="cancel")
                return
            yield AcpEventDone(stop_reason="stop")
        finally:
            uninstall()

    def _install_tool_hooks(self, telemetry: _ToolTelemetry):
        """Register before/after_tool_use taps on the agent's HookRunner.

        Returns an `uninstall()` callable that removes the same hooks
        — must be invoked in a `finally` block so a torn turn doesn't
        leak hooks across sessions.
        """
        runner: HookRunner | None = getattr(self._agent, "_hooks", None)
        if not isinstance(runner, HookRunner):
            # Agent doesn't expose a HookRunner (or has none). Telemetry
            # silently degrades — text deltas still flow, just no
            # tool_call notifications.
            def _noop_uninstall() -> None:
                return None

            return _noop_uninstall
        before = telemetry.make_before_hook()
        after = telemetry.make_after_hook()
        runner.before_tool_use.append(before)
        runner.after_tool_use.append(after)

        def uninstall() -> None:
            try:
                runner.before_tool_use.remove(before)
            except ValueError:  # pragma: no cover — already removed
                pass
            try:
                runner.after_tool_use.remove(after)
            except ValueError:  # pragma: no cover — already removed
                pass

        return uninstall

    async def cancel(self, *, handle: AcpRuntimeHandle, reason: str | None = None) -> None:
        state = self._sessions.get(handle.session_key)
        if state is None:
            return
        state.cancelled.set()

    async def close(
        self,
        *,
        handle: AcpRuntimeHandle,
        reason: str,
        discard_persistent_state: bool = False,
    ) -> None:
        state = self._sessions.pop(handle.session_key, None)
        if state is None:
            return
        state.closed = True
        if discard_persistent_state:
            # Best-effort: ask PiAgent to forget the recall snapshot
            # for this key. Underlying transcript is left alone — the
            # operator must delete the JSON file explicitly.
            invalidate = getattr(self._agent, "invalidate_recall_snapshot", None)
            if callable(invalidate):
                try:
                    invalidate(handle.session_key)
                except Exception:  # pragma: no cover — defensive
                    logger.warning(
                        "invalidate_recall_snapshot failed for %s",
                        handle.session_key,
                    )


__all__ = ["PiAgentAcpRuntime"]
