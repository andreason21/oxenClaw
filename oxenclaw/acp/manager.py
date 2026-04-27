"""AcpSessionManager — control plane for ACP sessions.

Singleton that owns the live `session_key → AcpRuntimeHandle` map,
routes `ensure_session` / `run_turn` / `cancel` / `close` to the
registered backend, and keeps a basic observability snapshot.

Ports the *minimum* useful slice of openclaw `manager.core.ts` (full
file is 2200+ LOC, most of which is gateway-side wiring we don't have
yet). What we keep:

  - one `AcpRuntimeHandle` per `session_key`
  - backend selection by id (defaults to the first healthy backend)
  - structured `initialize_session` → `run_turn` → `close_session` flow
  - `cancel_session` for in-flight aborts
  - `__testing__` reset hook for pytest isolation

What we skip (for later commits):

  - sub-agent depth/child caps + allow-list policy gates
  - thread/channel binding (`prepareAcpThreadBinding`)
  - idempotency tracking + run-id correlation
  - rate limit / 2 MB prompt cap / startup identity reconcile

Everything is `asyncio`-only; no thread pools.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.acp.runtime_registry import (
    AcpRegistryError,
    AcpRuntimeBackend,
    get_acp_runtime_backend,
    require_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimePromptMode,
    AcpRuntimeSessionMode,
    AcpRuntimeTurnAttachment,
    AcpRuntimeTurnInput,
)


class AcpManagerError(Exception):
    """Raised when a manager operation fails (unknown session, etc.)."""


@dataclass(frozen=True)
class AcpInitializeSessionInput:
    session_key: str
    agent: str
    mode: AcpRuntimeSessionMode = "persistent"
    backend_id: str | None = None
    resume_session_id: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class AcpRunTurnInput:
    session_key: str
    text: str
    request_id: str
    mode: AcpRuntimePromptMode = "prompt"
    attachments: list[AcpRuntimeTurnAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class AcpCloseSessionInput:
    session_key: str
    reason: str = "client_close"
    discard_persistent_state: bool = False


@dataclass(frozen=True)
class AcpManagerObservabilitySnapshot:
    sessions: dict[str, str]  # session_key → backend_id
    backends: list[str]


@dataclass
class _SessionRecord:
    handle: AcpRuntimeHandle
    backend: AcpRuntimeBackend


class AcpSessionManager:
    """Owns the live ACP session table for one process."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionRecord] = {}
        self._lock = asyncio.Lock()

    async def initialize_session(
        self, input: AcpInitializeSessionInput
    ) -> AcpRuntimeHandle:
        """Open (or resume) an ACP session against a backend.

        If the session_key is already live, the existing handle is
        returned — backends are responsible for resuming their own
        persistent state via `resume_session_id`. Reopening with a
        *different* backend_id raises rather than silently swap.
        """
        if not input.session_key:
            raise AcpManagerError("session_key is required")
        async with self._lock:
            existing = self._sessions.get(input.session_key)
            if existing is not None:
                if (
                    input.backend_id
                    and existing.backend.id != input.backend_id.lower()
                ):
                    raise AcpManagerError(
                        f"session {input.session_key!r} is bound to backend "
                        f"{existing.backend.id!r}, refusing rebind to "
                        f"{input.backend_id!r}"
                    )
                return existing.handle
            backend = require_acp_runtime_backend(input.backend_id)
            handle = await backend.runtime.ensure_session(
                AcpRuntimeEnsureInput(
                    session_key=input.session_key,
                    agent=input.agent,
                    mode=input.mode,
                    resume_session_id=input.resume_session_id,
                    cwd=input.cwd,
                    env=input.env,
                )
            )
            self._sessions[input.session_key] = _SessionRecord(
                handle=handle, backend=backend
            )
            return handle

    def run_turn(self, input: AcpRunTurnInput) -> AsyncIterator[AcpRuntimeEvent]:
        """Stream events for one prompt turn.

        Returns an async iterator. The caller is responsible for
        consuming or cancelling — the manager does not hold a
        reference to the in-flight generator.
        """
        record = self._sessions.get(input.session_key)
        if record is None:
            raise AcpManagerError(
                f"session {input.session_key!r} is not initialised"
            )
        return record.backend.runtime.run_turn(
            AcpRuntimeTurnInput(
                handle=record.handle,
                text=input.text,
                mode=input.mode,
                request_id=input.request_id,
                attachments=list(input.attachments),
            )
        )

    async def cancel_session(
        self, *, session_key: str, reason: str | None = None
    ) -> None:
        record = self._sessions.get(session_key)
        if record is None:
            raise AcpManagerError(
                f"session {session_key!r} is not initialised"
            )
        await record.backend.runtime.cancel(
            handle=record.handle, reason=reason
        )

    async def close_session(self, input: AcpCloseSessionInput) -> None:
        async with self._lock:
            record = self._sessions.pop(input.session_key, None)
        if record is None:
            return
        await record.backend.runtime.close(
            handle=record.handle,
            reason=input.reason,
            discard_persistent_state=input.discard_persistent_state,
        )

    def observability_snapshot(self) -> AcpManagerObservabilitySnapshot:
        return AcpManagerObservabilitySnapshot(
            sessions={
                key: rec.backend.id for key, rec in self._sessions.items()
            },
            backends=[rec.backend.id for rec in self._sessions.values()],
        )

    def has_session(self, session_key: str) -> bool:
        return session_key in self._sessions


_SINGLETON: AcpSessionManager | None = None


def get_acp_session_manager() -> AcpSessionManager:
    """Return the process-wide manager, lazy-creating it on first access."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = AcpSessionManager()
    return _SINGLETON


def reset_for_tests() -> None:
    """Drop the singleton. Test-only."""
    global _SINGLETON
    _SINGLETON = None


__all__ = [
    "AcpCloseSessionInput",
    "AcpInitializeSessionInput",
    "AcpManagerError",
    "AcpManagerObservabilitySnapshot",
    "AcpRunTurnInput",
    "AcpSessionManager",
    "get_acp_session_manager",
    "reset_for_tests",
]
