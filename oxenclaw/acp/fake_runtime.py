"""In-memory fake `AcpRuntime` for tests + `oxenclaw acp doctor`.

This is the only `AcpRuntime` implementation that ships in core.
Real backends (codex/claude/gemini ACP servers) live in extensions.
The fake is useful for:

  - Unit tests that exercise the manager + registry surface without
    spawning subprocesses or wiring real wire I/O.
  - `oxenclaw acp doctor` smoke checks (later commit).
  - Documentation examples.

The fake does NOT implement the full `AcpRuntimeOptional` surface
on purpose — we want at least one backend in the tree that
exercises the *required-only* path so future Protocol drift
breaks the build immediately.

Behaviour:

  - `ensure_session` → returns a handle keyed by the requested
    session_key, backend "fake".
  - `run_turn` → yields one `text_delta` echoing the prompt, one
    `done(stop_reason='stop')`, then completes. If a `script` is
    pre-loaded for the session_key via `script_session(...)`, that
    sequence is yielded instead.
  - `cancel` → sets a cancel flag the next `run_turn` checks at
    every yield boundary.
  - `close` → drops the per-session state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventTextDelta,
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimeTurnInput,
)


@dataclass
class _FakeSessionState:
    handle: AcpRuntimeHandle
    cancelled: bool = False
    closed: bool = False
    scripted_events: list[AcpRuntimeEvent] = field(default_factory=list)


class InMemoryFakeRuntime:
    """Backend id "fake". See module docstring for behaviour."""

    backend_id: str = "fake"

    def __init__(self) -> None:
        self._sessions: dict[str, _FakeSessionState] = {}
        # Counter to mint distinct runtime_session_name values.
        self._counter = 0

    # ---- AcpRuntime required surface --------------------------------------

    async def ensure_session(self, input: AcpRuntimeEnsureInput) -> AcpRuntimeHandle:
        existing = self._sessions.get(input.session_key)
        if existing is not None and not existing.closed:
            return existing.handle
        self._counter += 1
        handle = AcpRuntimeHandle(
            session_key=input.session_key,
            backend=self.backend_id,
            runtime_session_name=f"fake-{self._counter:04d}",
            cwd=input.cwd,
            agent_session_id=input.resume_session_id,
        )
        self._sessions[input.session_key] = _FakeSessionState(handle=handle)
        return handle

    def run_turn(self, input: AcpRuntimeTurnInput) -> AsyncIterator[AcpRuntimeEvent]:
        return self._run_turn(input)

    async def _run_turn(self, input: AcpRuntimeTurnInput) -> AsyncIterator[AcpRuntimeEvent]:
        state = self._sessions.get(input.handle.session_key)
        if state is None or state.closed:
            return
        # `cancelled` is honoured if it was set before this turn (pre-cancel)
        # or during a yield boundary inside the turn. We clear it AFTER the
        # cancel-done emission so the next turn starts fresh.
        if state.scripted_events:
            for ev in list(state.scripted_events):
                if state.cancelled:
                    state.cancelled = False
                    yield AcpEventDone(stop_reason="cancel")
                    return
                yield ev
                # Yield to the event loop so cancel() can land.
                await asyncio.sleep(0)
            return
        # Default echo behaviour.
        if state.cancelled:
            state.cancelled = False
            yield AcpEventDone(stop_reason="cancel")
            return
        yield AcpEventTextDelta(text=input.text, stream="output")
        await asyncio.sleep(0)
        if state.cancelled:
            state.cancelled = False
            yield AcpEventDone(stop_reason="cancel")
            return
        yield AcpEventDone(stop_reason="stop")

    async def cancel(self, *, handle: AcpRuntimeHandle, reason: str | None = None) -> None:
        state = self._sessions.get(handle.session_key)
        if state is None:
            return
        state.cancelled = True

    async def close(
        self,
        *,
        handle: AcpRuntimeHandle,
        reason: str,
        discard_persistent_state: bool = False,
    ) -> None:
        state = self._sessions.pop(handle.session_key, None)
        if state is not None:
            state.closed = True

    # ---- test helpers (NOT part of AcpRuntime) ---------------------------

    def script_session(self, session_key: str, events: list[AcpRuntimeEvent]) -> None:
        """Override the default echo behaviour for one session.

        Subsequent `run_turn` calls will yield `events` in order.
        Useful for exercising specific event sequences in tests.
        """
        state = self._sessions.get(session_key)
        if state is None:
            raise KeyError(f"session {session_key!r} must be ensured before scripting")
        state.scripted_events = list(events)


__all__ = ["InMemoryFakeRuntime"]
