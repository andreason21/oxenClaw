"""Session lifecycle: reset, fork, archive, events.

Mirrors `openclaw/src/config/sessions/reset*.ts` +
`auto-reply/reply/session-fork.ts` + `gateway/session-archive.fs.ts` +
`sessions/session-lifecycle-events.ts`.

- `reset_session(...)`     — wipe transcript (full) or drop after a turn
                             index (partial), with a `preserved` set for
                             system + recent N user turns.
- `fork_session(...)`      — copy a session up to a turn index into a new
                             session id (so the model can re-answer from
                             the same context without losing the original).
- `archive_session(...)`   — gzip the transcript JSON to `archive_dir`
                             and delete the live row, returning the path.
- `LifecycleBus`           — pub/sub for session events; PiAgent /
                             dispatcher / dashboards subscribe.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sampyclaw.pi.messages import (
    AgentMessage,
    SystemMessage,
    UserMessage,
)
from sampyclaw.pi.session import (
    AgentSession,
    CompactionEntry,
    CreateAgentSessionOptions,
    SessionManager,
)
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.lifecycle")


# ─── Lifecycle events ────────────────────────────────────────────────


class LifecycleEvent(StrEnum):
    CREATED = "session.created"
    UPDATED = "session.updated"
    RESET = "session.reset"
    FORKED = "session.forked"
    ARCHIVED = "session.archived"
    DELETED = "session.deleted"


EventHandler = Callable[[LifecycleEvent, dict[str, Any]], Awaitable[None]]


class LifecycleBus:
    """Trivial in-process pub/sub.

    Subscribers register a coroutine. `emit()` fans out concurrently and
    swallows per-subscriber failures so one slow handler can't block the
    rest.
    """

    def __init__(self) -> None:
        self._subs: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        self._subs.append(handler)

    def unsubscribe(self, handler: EventHandler) -> bool:
        try:
            self._subs.remove(handler)
            return True
        except ValueError:
            return False

    async def emit(self, kind: LifecycleEvent, payload: dict[str, Any]) -> None:
        if not self._subs:
            return

        async def _safe(h: EventHandler) -> None:
            try:
                await h(kind, payload)
            except Exception:
                logger.exception("lifecycle subscriber failed for %s", kind)

        await asyncio.gather(*(_safe(h) for h in self._subs))


# ─── Reset ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResetPolicy:
    """How aggressive a reset should be.

    - `full=True`: drop all messages.
    - `keep_system=True`: even on full reset, keep the leading SystemMessage
      so the agent's persona survives.
    - `keep_last_user_turns`: preserve the last N user turns + their
      assistant replies (useful for "trim history" operations).
    - `keep_compactions=True`: preserve the CompactionEntry log even when
      messages are wiped (audit trail).
    """

    full: bool = True
    keep_system: bool = True
    keep_last_user_turns: int = 0
    keep_compactions: bool = True


def _is_user(msg: AgentMessage) -> bool:
    return isinstance(msg, UserMessage)


def reset_session_messages(session: AgentSession, policy: ResetPolicy) -> int:
    """Apply `policy` to `session.messages` in-place. Returns drop count.

    On a partial reset, walks back from the tail keeping `keep_last_user_turns`
    user turns plus everything after them; the rest is dropped (system stays
    if `keep_system`).
    """
    before = len(session.messages)
    if policy.full and policy.keep_last_user_turns <= 0:
        kept: list[AgentMessage] = []
        if (
            policy.keep_system
            and session.messages
            and isinstance(session.messages[0], SystemMessage)
        ):
            kept.append(session.messages[0])
        session.messages = kept
    else:
        # Walk from end collecting turns until we have N user messages.
        target = max(1, policy.keep_last_user_turns)
        kept_tail: list[AgentMessage] = []
        seen_users = 0
        for msg in reversed(session.messages):
            kept_tail.append(msg)
            if _is_user(msg):
                seen_users += 1
                if seen_users >= target:
                    break
        kept_tail.reverse()
        head: list[AgentMessage] = []
        if (
            policy.keep_system
            and session.messages
            and isinstance(session.messages[0], SystemMessage)
        ):
            head.append(session.messages[0])
        session.messages = head + kept_tail
    if not policy.keep_compactions:
        session.compactions = []
    return before - len(session.messages)


async def reset_session(
    sm: SessionManager,
    session_id: str,
    *,
    policy: ResetPolicy | None = None,
    bus: LifecycleBus | None = None,
) -> AgentSession | None:
    """Load → reset → save. Returns the updated session (None if missing)."""
    session = await sm.get(session_id)
    if session is None:
        return None
    pol = policy or ResetPolicy()
    dropped = reset_session_messages(session, pol)
    await sm.save(session)
    if bus is not None:
        await bus.emit(
            LifecycleEvent.RESET,
            {
                "id": session.id,
                "agent_id": session.agent_id,
                "dropped": dropped,
                "policy": {
                    "full": pol.full,
                    "keep_system": pol.keep_system,
                    "keep_last_user_turns": pol.keep_last_user_turns,
                    "keep_compactions": pol.keep_compactions,
                },
            },
        )
    return session


# ─── Fork ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForkOptions:
    """Where in the source transcript to split."""

    # Inclusive index — new session keeps `messages[: until_index + 1]`.
    until_index: int | None = None
    # New session title; defaults to "fork of <source title>".
    title: str | None = None
    # Carry over the source compaction history (audit) into the fork.
    carry_compactions: bool = True


async def fork_session(
    sm: SessionManager,
    source_id: str,
    *,
    options: ForkOptions | None = None,
    bus: LifecycleBus | None = None,
) -> AgentSession | None:
    """Create a new session that branches from `source_id`.

    The new session keeps everything up to `until_index` (inclusive). When
    None, copies the full transcript so the fork starts identical and the
    next user turn diverges.
    """
    src = await sm.get(source_id)
    if src is None:
        return None
    opts = options or ForkOptions()
    end = opts.until_index if opts.until_index is not None else len(src.messages) - 1
    end = max(-1, min(end, len(src.messages) - 1))
    forked_messages = list(src.messages[: end + 1])

    new = await sm.create(
        CreateAgentSessionOptions(
            agent_id=src.agent_id,
            model_id=src.model_id,
            title=opts.title or f"fork of {src.title or src.id[:8]}",
            metadata={**src.metadata, "forked_from": src.id, "forked_at_index": end},
        )
    )
    new.messages = forked_messages
    if opts.carry_compactions:
        new.compactions = list(src.compactions)
    await sm.save(new)

    if bus is not None:
        await bus.emit(
            LifecycleEvent.FORKED,
            {
                "source_id": src.id,
                "new_id": new.id,
                "until_index": end,
                "message_count": len(forked_messages),
            },
        )
    return new


# ─── Archive ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveResult:
    archive_path: Path
    bytes_written: int


async def archive_session(
    sm: SessionManager,
    session_id: str,
    *,
    archive_dir: Path,
    delete_after: bool = True,
    bus: LifecycleBus | None = None,
) -> ArchiveResult | None:
    """Serialise the session to a gzipped JSON file under `archive_dir`,
    then optionally delete the live row.

    File name: `<agent_id>__<session_id>__<ts>.json.gz`. The format is
    forward-compatible: a single JSON object with `session`, `messages`,
    `compactions` keys mirroring AgentSession.
    """
    session = await sm.get(session_id)
    if session is None:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    fname = f"{session.agent_id}__{session.id}__{ts}.json.gz"
    out_path = archive_dir / fname

    payload = {
        "session": {
            "id": session.id,
            "agent_id": session.agent_id,
            "model_id": session.model_id,
            "title": session.title,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "metadata": session.metadata,
        },
        "messages": [m.model_dump() for m in session.messages],
        "compactions": [
            {
                "id": e.id,
                "summary": e.summary,
                "replaced_message_indexes": list(e.replaced_message_indexes),
                "created_at": e.created_at,
                "reason": e.reason,
                "tokens_before": e.tokens_before,
                "tokens_after": e.tokens_after,
                "original_archive_path": e.original_archive_path,
            }
            for e in session.compactions
        ],
        "archived_at": time.time(),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with gzip.open(out_path, "wb") as fh:
        fh.write(raw)
    bytes_written = out_path.stat().st_size

    if delete_after:
        await sm.delete(session.id)

    if bus is not None:
        await bus.emit(
            LifecycleEvent.ARCHIVED,
            {
                "id": session.id,
                "agent_id": session.agent_id,
                "archive_path": str(out_path),
                "bytes": bytes_written,
                "deleted": delete_after,
            },
        )
    return ArchiveResult(archive_path=out_path, bytes_written=bytes_written)


async def restore_archive(sm: SessionManager, archive_path: Path) -> AgentSession | None:
    """Inverse of `archive_session`. Reads the gzipped JSON and re-creates
    a session row. Returns the new session (the original id is preserved
    — if a row with that id already exists, it is overwritten via save())."""
    if not archive_path.exists():
        return None
    with gzip.open(archive_path, "rb") as fh:
        payload = json.loads(fh.read().decode("utf-8"))
    snap = payload.get("session", {})
    new = AgentSession(
        id=snap["id"],
        agent_id=snap["agent_id"],
        model_id=snap.get("model_id"),
        title=snap.get("title"),
        created_at=float(snap.get("created_at", time.time())),
        updated_at=float(snap.get("updated_at", time.time())),
        metadata=snap.get("metadata") or {},
    )
    from pydantic import TypeAdapter

    new.messages = [
        TypeAdapter(AgentMessage).validate_python(m) for m in payload.get("messages") or []
    ]
    new.compactions = [
        CompactionEntry(
            id=e["id"],
            summary=e["summary"],
            replaced_message_indexes=tuple(e["replaced_message_indexes"]),
            created_at=float(e["created_at"]),
            reason=e["reason"],
            tokens_before=int(e["tokens_before"]),
            tokens_after=int(e["tokens_after"]),
            original_archive_path=e.get("original_archive_path"),
        )
        for e in payload.get("compactions") or []
    ]
    await sm.save(new)
    return new


__all__ = [
    "ArchiveResult",
    "EventHandler",
    "ForkOptions",
    "LifecycleBus",
    "LifecycleEvent",
    "ResetPolicy",
    "archive_session",
    "fork_session",
    "reset_session",
    "reset_session_messages",
    "restore_archive",
]
