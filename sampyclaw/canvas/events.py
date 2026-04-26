"""CanvasEventBus — pub/sub between RPC layer and dashboard subscribers.

The gateway exposes a `canvas.subscribe` push channel that streams
`CanvasEvent`s to every connected dashboard. The bus is a thin asyncio
fanout that:

- Wraps every subscriber in a bounded `asyncio.Queue` so a slow
  dashboard tab can't block other subscribers.
- Drops events when a subscriber's queue is full (with a warning),
  rather than blocking the publisher.
- Swallows subscriber cleanup errors so a misbehaving consumer can't
  leak into the publisher's call stack.

`canvas.eval` is request/response, not pure pub/sub: the publisher
issues a `CanvasEvent(kind="eval", request_id=...)`, the dashboard runs
the JS in its sandboxed iframe and POSTs back to `canvas.eval_result`,
which resolves the Future the publisher is awaiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("canvas.events")


DEFAULT_QUEUE_SIZE = 64


@dataclass
class CanvasEvent:
    """Server -> dashboard push payload."""

    kind: str  # "present" | "hide" | "navigate" | "eval"
    agent_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None  # set when an eval response is expected
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "agent_id": self.agent_id,
            "payload": self.payload,
            "request_id": self.request_id,
            "ts": self.ts,
        }


class CanvasEventBus:
    """In-process pub/sub for canvas events."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subs: set[asyncio.Queue[CanvasEvent]] = set()
        self._lock = asyncio.Lock()
        self._eval_waiters: dict[str, asyncio.Future[Any]] = {}

    # ─── publish ───────────────────────────────────────────────────

    def publish(self, event: CanvasEvent) -> int:
        """Fanout `event` to every subscriber. Returns delivered count."""
        delivered = 0
        for q in list(self._subs):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning(
                    "canvas subscriber queue full; dropping event kind=%s agent=%s",
                    event.kind,
                    event.agent_id,
                )
        return delivered

    # ─── subscribe ─────────────────────────────────────────────────

    async def subscribe(self) -> asyncio.Queue[CanvasEvent]:
        """Add a new subscriber. Caller is responsible for `unsubscribe`."""
        q: asyncio.Queue[CanvasEvent] = asyncio.Queue(maxsize=self._queue_size)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[CanvasEvent]) -> None:
        async with self._lock:
            self._subs.discard(q)

    async def stream(self) -> AsyncIterator[CanvasEvent]:
        """Convenience iterator. Stops when caller breaks the loop."""
        q = await self.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            await self.unsubscribe(q)

    # ─── eval request/response ─────────────────────────────────────

    def new_eval_request_id(self) -> str:
        return uuid4().hex

    def register_eval_waiter(
        self, request_id: str, *, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.Future[Any]:
        """Create a Future the publisher will await for the eval result."""
        fut: asyncio.Future[Any] = (loop or asyncio.get_event_loop()).create_future()
        self._eval_waiters[request_id] = fut
        return fut

    def resolve_eval(self, request_id: str, result: Any) -> bool:
        fut = self._eval_waiters.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(result)
        return True

    def reject_eval(self, request_id: str, error: BaseException) -> bool:
        fut = self._eval_waiters.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_exception(error)
        return True

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


__all__ = [
    "DEFAULT_QUEUE_SIZE",
    "CanvasEvent",
    "CanvasEventBus",
]
