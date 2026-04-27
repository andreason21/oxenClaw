"""Concurrency lanes for the agent dispatcher.

Two-level serialisation, mirroring openclaw `enqueueGlobal` +
`enqueueSession`:

  - **Session lane**: at most one in-flight turn per `(agent_id,
    session_key)`. Two concurrent `chat.send` calls on the same
    session queue and run in arrival order. Prevents overlapping
    history writes from corrupting the JSON file.
  - **Global lane**: optional cap on total in-flight turns across
    all sessions. Keeps a 4-vCPU host from getting torched when
    twenty channels light up at once.

Both lanes are asyncio-only — no thread pools, no SQLite locks.
The dispatcher already runs everything on the same loop.

Busy policy. `BusyPolicy` controls what happens when a second envelope
arrives for a `(agent_id, session_key)` lane that already has a turn
in flight:

  - ``"queue"``  (default, current behaviour): hold the second message
    behind the lane lock — it runs after the first finishes.
  - ``"block"``: same lock semantics but explicit; reserved for
    operators that want symmetric naming with hermes.
  - ``"interrupt"``: signal `abort_event` on the running turn and start
    the new one as soon as the lock releases.
  - ``"steer"``: (best-effort) inject the new user message as a
    mid-stream nudge into the running turn; falls back to queue.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

T = TypeVar("T")

BusyPolicy = Literal["block", "queue", "interrupt", "steer"]


@dataclass
class LaneState:
    """Per-(agent_id, session_key) bookkeeping used by the busy policy."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    abort_event: asyncio.Event | None = None
    pending_messages: list[Any] = field(default_factory=list)
    queued_at: float | None = None
    in_flight_iter: int = 0
    in_flight_tool: str = ""


_BUSY_ACK_DEBOUNCE_S = 30.0


class LaneRegistry:
    """Holds per-key locks (session lane) + a global semaphore.

    Session locks are created lazily and never garbage-collected — the
    set grows with the number of distinct sessions seen. Each lock is
    a few hundred bytes; a long-running gateway will accumulate them
    but stay well under MB-scale even at thousands of sessions.
    """

    def __init__(
        self,
        *,
        global_concurrency: int | None = None,
        busy_policy: BusyPolicy = "queue",
    ) -> None:
        self._lanes: dict[tuple[str, str], LaneState] = {}
        # `None` → unlimited (effective default; matches pre-port behaviour).
        self._global_sem: asyncio.Semaphore | None = (
            asyncio.Semaphore(global_concurrency)
            if global_concurrency and global_concurrency > 0
            else None
        )
        self._global_concurrency = global_concurrency
        self._busy_policy: BusyPolicy = busy_policy
        # Last "still busy" ack we surfaced for a lane — debounced to
        # avoid spamming the dashboard every 200 ms.
        self._last_busy_ack: dict[tuple[str, str], float] = {}

    @property
    def global_concurrency(self) -> int | None:
        return self._global_concurrency

    @property
    def busy_policy(self) -> BusyPolicy:
        return self._busy_policy

    def lane(self, agent_id: str, session_key: str) -> LaneState:
        key = (agent_id, session_key)
        state = self._lanes.get(key)
        if state is None:
            state = LaneState()
            self._lanes[key] = state
        return state

    # Back-compat: callers expect a raw asyncio.Lock from `_session_lock`.
    def _session_lock(self, agent_id: str, session_key: str) -> asyncio.Lock:
        return self.lane(agent_id, session_key).lock

    def maybe_busy_ack(
        self, agent_id: str, session_key: str
    ) -> tuple[bool, LaneState]:
        """Return `(should_emit, lane_state)` — True if the caller should
        push a "still busy" status event for this lane.

        Debounced to once every `_BUSY_ACK_DEBOUNCE_S`. The first call
        on an idle lane returns False (no need to ack — there's no
        in-flight turn). The first call after the configurable debounce
        threshold returns True; subsequent calls inside the window
        return False.
        """
        state = self.lane(agent_id, session_key)
        if not state.lock.locked():
            return False, state
        now = time.monotonic()
        queued_at = state.queued_at or now
        if (now - queued_at) < _BUSY_ACK_DEBOUNCE_S:
            return False, state
        last = self._last_busy_ack.get((agent_id, session_key), 0.0)
        if (now - last) < _BUSY_ACK_DEBOUNCE_S:
            return False, state
        self._last_busy_ack[(agent_id, session_key)] = now
        return True, state

    def queue_message(self, agent_id: str, session_key: str, message: Any) -> None:
        state = self.lane(agent_id, session_key)
        if state.queued_at is None:
            state.queued_at = time.monotonic()
        state.pending_messages.append(message)

    def signal_abort(self, agent_id: str, session_key: str) -> bool:
        """Set the lane's `abort_event` if one is registered; return True if signalled."""
        state = self.lane(agent_id, session_key)
        if state.abort_event is not None and not state.abort_event.is_set():
            state.abort_event.set()
            return True
        return False

    async def run(
        self,
        *,
        agent_id: str,
        session_key: str,
        coro_factory: Callable[[], Awaitable[T]],
    ) -> T:
        """Run `await coro_factory()` under the matching session lane
        (and the global cap if configured).

        `coro_factory` is a thunk so the coroutine is created INSIDE
        the lock (the canonical asyncio pattern; otherwise the
        coroutine eagerly evaluates and we lose ordering).
        """
        state = self.lane(agent_id, session_key)
        if state.lock.locked() and self._busy_policy == "interrupt":
            # Signal abort before queueing so the in-flight turn knows to
            # bail out as soon as it can. The new turn still has to wait
            # for the lock to release.
            self.signal_abort(agent_id, session_key)
        # Track when this caller began waiting so the first ~30s pass
        # silently and a follow-up status event can fire if the lane
        # hasn't drained yet.
        if state.queued_at is None and state.lock.locked():
            state.queued_at = time.monotonic()
        if self._global_sem is None:
            async with state.lock:
                state.queued_at = None
                return await coro_factory()
        async with self._global_sem, state.lock:
            state.queued_at = None
            return await coro_factory()

    def stats(self) -> dict[str, Any]:
        """Inspector view for an `agents.lanes` RPC or healthcheck."""
        held_sessions = sum(1 for s in self._lanes.values() if s.lock.locked())
        out: dict[str, Any] = {
            "session_lock_count": len(self._lanes),
            "session_locks_held": held_sessions,
            "global_concurrency": self._global_concurrency,
            "busy_policy": self._busy_policy,
        }
        if self._global_sem is not None and self._global_concurrency:
            # asyncio.Semaphore exposes `_value` for read-only inspection;
            # acceptable for diagnostics.
            available = getattr(self._global_sem, "_value", None)
            out["global_available"] = available
            out["global_in_flight"] = (
                self._global_concurrency - available if isinstance(available, int) else None
            )
        return out


__all__ = ["BusyPolicy", "LaneRegistry", "LaneState"]
