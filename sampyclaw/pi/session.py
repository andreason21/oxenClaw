"""Session-level types from `@mariozechner/pi-coding-agent`.

`AgentSession` is the persistent state of one conversation; `SessionManager`
owns the on-disk store and CRUD over sessions. `SessionEntry` /
`CompactionEntry` are the on-disk row shapes.

Phase 1 ships the type/shape definitions and an in-memory SessionManager
that satisfies the contract. Phase 6 will replace the in-memory backing
with a sqlite-backed implementation that also handles compaction history,
matching openclaw's persistence guarantees.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

if TYPE_CHECKING:
    from sampyclaw.pi.models import Model


@dataclass
class AgentSession:
    """One conversation. `messages` is the canonical transcript; the runner
    appends to it after each turn. `compactions` records every time the
    transcript was rewritten (summary + indexes) so replay can reconstruct
    state."""

    id: str = field(default_factory=lambda: uuid4().hex)
    title: str | None = None
    agent_id: str = "default"
    model_id: str | None = None
    messages: list[Any] = field(default_factory=list)  # list[AgentMessage]
    compactions: list[CompactionEntry] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionEntry:
    """Row returned by `SessionManager.list()` — light-weight summary."""

    id: str
    title: str | None
    agent_id: str
    model_id: str | None
    message_count: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class CompactionEntry:
    """One compaction event in a session's history.

    `replaced_message_indexes` are the original positions in `session.messages`
    that the summary now stands in for; replay can reconstruct full history
    by re-fetching from `original_archive_path` if present.
    """

    id: str
    summary: str
    replaced_message_indexes: tuple[int, ...]
    created_at: float
    reason: str  # "auto" | "manual" | "overflow" | "timeout"
    tokens_before: int
    tokens_after: int
    original_archive_path: str | None = None


@dataclass(frozen=True)
class CreateAgentSessionOptions:
    """Constructor knobs for `SessionManager.create()`."""

    agent_id: str = "default"
    model_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] | None = None


class SettingsManager(Protocol):
    """Per-agent settings store. The runner reads compaction thresholds,
    thinking level, and similar knobs through this surface so settings can
    live in any backend."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


class SessionManager(Protocol):
    """Async CRUD over agent sessions."""

    async def create(self, opts: CreateAgentSessionOptions) -> AgentSession: ...

    async def get(self, session_id: str) -> AgentSession | None: ...

    async def list(self, *, agent_id: str | None = None) -> list[SessionEntry]: ...

    async def save(self, session: AgentSession) -> None: ...

    async def delete(self, session_id: str) -> bool: ...


class InMemorySessionManager:
    """Reference implementation backing for tests and for Phase 1.

    Phase 6 swaps this for a sqlite-backed implementation that also handles
    compaction-archive storage. The Protocol above stays stable.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}

    async def create(self, opts: CreateAgentSessionOptions) -> AgentSession:
        s = AgentSession(
            agent_id=opts.agent_id,
            model_id=opts.model_id,
            title=opts.title,
            metadata=dict(opts.metadata or {}),
        )
        self._sessions[s.id] = s
        return s

    async def get(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    async def list(self, *, agent_id: str | None = None) -> list[SessionEntry]:
        out: list[SessionEntry] = []
        for s in self._sessions.values():
            if agent_id is not None and s.agent_id != agent_id:
                continue
            out.append(
                SessionEntry(
                    id=s.id,
                    title=s.title,
                    agent_id=s.agent_id,
                    model_id=s.model_id,
                    message_count=len(s.messages),
                    created_at=s.created_at,
                    updated_at=s.updated_at,
                )
            )
        return out

    async def save(self, session: AgentSession) -> None:
        session.updated_at = time.time()
        self._sessions[session.id] = session

    async def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None


# `ExtensionFactory` is a callable that builds tools/skills bound to a
# specific session/model. The runner invokes it once per attempt.
ExtensionFactory = Callable[[AgentSession, "Model"], Awaitable[list[Any]]]


__all__ = [
    "AgentSession",
    "CompactionEntry",
    "CreateAgentSessionOptions",
    "ExtensionFactory",
    "InMemorySessionManager",
    "SessionEntry",
    "SessionManager",
    "SettingsManager",
]
