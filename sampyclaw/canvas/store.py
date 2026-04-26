"""CanvasStore — last-known canvas state per agent.

The dashboard is the only render target, so we only ever care about the
**most recent** canvas state per agent. Older states are dropped — there
is no history. This keeps memory bounded without an LRU on time and
makes the "what is currently shown" query O(1).

We do cap the number of distinct agents we track via an LRU eviction so
a long-lived gateway with many agents created/destroyed can't grow
unbounded.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

DEFAULT_AGENT_CAPACITY = 16
ABSOLUTE_MAX_HTML_BYTES = 1_048_576  # 1 MiB — refuse anything bigger at API edge


@dataclass
class CanvasState:
    """Snapshot of one agent's current canvas."""

    html: str = ""
    title: str = ""
    version: int = 0
    updated_at: float = field(default_factory=time.time)
    hidden: bool = True

    def to_dict(self) -> dict:
        return {
            "html": self.html,
            "title": self.title,
            "version": self.version,
            "updated_at": self.updated_at,
            "hidden": self.hidden,
        }


class CanvasStore:
    """Thread-safe per-agent canvas state with LRU eviction."""

    def __init__(self, *, capacity: int = DEFAULT_AGENT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._states: OrderedDict[str, CanvasState] = OrderedDict()

    # ─── mutators ──────────────────────────────────────────────────

    def present(self, agent_id: str, *, html: str, title: str = "") -> CanvasState:
        with self._lock:
            existing = self._states.get(agent_id)
            version = (existing.version + 1) if existing else 1
            state = CanvasState(
                html=html, title=title, version=version,
                updated_at=time.time(), hidden=False,
            )
            self._states[agent_id] = state
            self._states.move_to_end(agent_id)
            self._evict_if_needed()
            return state

    def hide(self, agent_id: str) -> CanvasState | None:
        with self._lock:
            state = self._states.get(agent_id)
            if state is None:
                return None
            state.hidden = True
            state.updated_at = time.time()
            self._states.move_to_end(agent_id)
            return state

    def clear(self, agent_id: str) -> None:
        with self._lock:
            self._states.pop(agent_id, None)

    # ─── read ──────────────────────────────────────────────────────

    def get(self, agent_id: str) -> CanvasState | None:
        with self._lock:
            state = self._states.get(agent_id)
            if state is None:
                return None
            self._states.move_to_end(agent_id)
            return state

    def known_agents(self) -> list[str]:
        with self._lock:
            return list(self._states.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    # ─── internal ──────────────────────────────────────────────────

    def _evict_if_needed(self) -> None:
        while len(self._states) > self._capacity:
            self._states.popitem(last=False)


__all__ = [
    "ABSOLUTE_MAX_HTML_BYTES",
    "DEFAULT_AGENT_CAPACITY",
    "CanvasState",
    "CanvasStore",
]
