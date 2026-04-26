"""sessions.* JSON-RPC methods.

Mirrors the openclaw `sessions-history-http`, `sessions-resolve`,
`sessions-patch`, `session-preview`, and `session-reset-service` surfaces.

Methods exposed:
- `sessions.list`          → list summaries (filter by agent_id)
- `sessions.get`           → full session with messages + compactions
- `sessions.preview`       → first/last user + assistant snippets
- `sessions.patch`         → update title / metadata / policy
- `sessions.reset`         → apply ResetPolicy
- `sessions.fork`          → branch into a new session
- `sessions.archive`       → gzip + delete
- `sessions.delete`        → drop a session
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.gateway.router import Router
from sampyclaw.pi.lifecycle import (
    ForkOptions,
    LifecycleBus,
    ResetPolicy,
    archive_session,
    fork_session,
    reset_session,
)
from sampyclaw.pi.policy import (
    deserialize_policy,
    set_policy,
)
from sampyclaw.pi.session import SessionManager

# ─── param models ────────────────────────────────────────────────────


class _ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None


class _GetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class _PreviewParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    head_chars: int = 200
    tail_chars: int = 200


class _PatchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str | None = None
    metadata: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None  # raw serialised policy


class _ResetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    full: bool = True
    keep_system: bool = True
    keep_last_user_turns: int = 0
    keep_compactions: bool = True


class _ForkParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    until_index: int | None = None
    title: str | None = None
    carry_compactions: bool = True


class _ArchiveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    delete_after: bool = True


class _DeleteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


# ─── helpers ─────────────────────────────────────────────────────────


def _entry_to_dict(e) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": e.id,
        "title": e.title,
        "agent_id": e.agent_id,
        "model_id": e.model_id,
        "message_count": e.message_count,
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


def _session_to_dict(s) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": s.id,
        "title": s.title,
        "agent_id": s.agent_id,
        "model_id": s.model_id,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
        "metadata": s.metadata,
        "messages": [m.model_dump() for m in s.messages],
        "compactions": [
            {
                "id": c.id,
                "summary": c.summary,
                "replaced_message_indexes": list(c.replaced_message_indexes),
                "created_at": c.created_at,
                "reason": c.reason,
                "tokens_before": c.tokens_before,
                "tokens_after": c.tokens_after,
            }
            for c in s.compactions
        ],
    }


def _preview(s, *, head: int, tail: int) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Cheap "first user / last assistant" summary used in list views."""
    first_user_text: str | None = None
    last_assistant_text: str | None = None
    for m in s.messages:
        if m.role == "user" and first_user_text is None:
            content = m.content
            if isinstance(content, str):
                first_user_text = content[:head]
            elif isinstance(content, list) and content:
                t = next((b for b in content if getattr(b, "type", None) == "text"), None)
                if t is not None:
                    first_user_text = t.text[:head]
            if first_user_text is not None:
                break
    for m in reversed(s.messages):
        if m.role == "assistant":
            text_blocks = [b.text for b in m.content if getattr(b, "type", None) == "text"]
            if text_blocks:
                last_assistant_text = text_blocks[-1][-tail:]
                break
    return {
        "id": s.id,
        "title": s.title,
        "first_user": first_user_text,
        "last_assistant": last_assistant_text,
        "message_count": len(s.messages),
        "compaction_count": len(s.compactions),
    }


# ─── registration ────────────────────────────────────────────────────


def register_sessions_methods(
    router: Router,
    sm: SessionManager,
    *,
    archive_dir: Path | None = None,
    bus: LifecycleBus | None = None,
    paths: SampyclawPaths | None = None,
) -> None:
    paths = paths or default_paths()
    archive_dir = archive_dir or (paths.home / "archives")

    @router.method("sessions.list", _ListParams)
    async def _list(p: _ListParams) -> list[dict[str, Any]]:
        rows = await sm.list(agent_id=p.agent_id)
        return [_entry_to_dict(r) for r in rows]

    @router.method("sessions.get", _GetParams)
    async def _get(p: _GetParams) -> dict[str, Any] | None:
        s = await sm.get(p.id)
        return _session_to_dict(s) if s else None

    @router.method("sessions.preview", _PreviewParams)
    async def _preview_method(p: _PreviewParams) -> dict[str, Any] | None:
        s = await sm.get(p.id)
        if s is None:
            return None
        return _preview(s, head=p.head_chars, tail=p.tail_chars)

    @router.method("sessions.patch", _PatchParams)
    async def _patch(p: _PatchParams) -> dict[str, Any] | None:
        s = await sm.get(p.id)
        if s is None:
            return None
        if p.title is not None:
            s.title = p.title
        if p.metadata is not None:
            s.metadata = {**s.metadata, **p.metadata}
        if p.policy is not None:
            set_policy(s, deserialize_policy(p.policy))
        await sm.save(s)
        return _entry_to_dict(
            type(
                "E",
                (),
                {  # tiny duck-typed entry
                    "id": s.id,
                    "title": s.title,
                    "agent_id": s.agent_id,
                    "model_id": s.model_id,
                    "message_count": len(s.messages),
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                },
            )()
        )

    @router.method("sessions.reset", _ResetParams)
    async def _reset(p: _ResetParams) -> dict[str, Any] | None:
        out = await reset_session(
            sm,
            p.id,
            policy=ResetPolicy(
                full=p.full,
                keep_system=p.keep_system,
                keep_last_user_turns=p.keep_last_user_turns,
                keep_compactions=p.keep_compactions,
            ),
            bus=bus,
        )
        if out is None:
            return None
        return {"id": out.id, "messages_remaining": len(out.messages)}

    @router.method("sessions.fork", _ForkParams)
    async def _fork(p: _ForkParams) -> dict[str, Any] | None:
        out = await fork_session(
            sm,
            p.id,
            options=ForkOptions(
                until_index=p.until_index,
                title=p.title,
                carry_compactions=p.carry_compactions,
            ),
            bus=bus,
        )
        if out is None:
            return None
        return {"id": out.id, "messages": len(out.messages), "title": out.title}

    @router.method("sessions.archive", _ArchiveParams)
    async def _archive(p: _ArchiveParams) -> dict[str, Any] | None:
        result = await archive_session(
            sm,
            p.id,
            archive_dir=archive_dir,
            delete_after=p.delete_after,
            bus=bus,
        )
        if result is None:
            return None
        return {
            "archive_path": str(result.archive_path),
            "bytes": result.bytes_written,
        }

    @router.method("sessions.delete", _DeleteParams)
    async def _delete(p: _DeleteParams) -> dict[str, Any]:
        ok = await sm.delete(p.id)
        return {"deleted": ok}


__all__ = ["register_sessions_methods"]
