"""Session management tools the LLM can call.

Exposes six FunctionTools that map directly to SessionManager operations,
mirroring openclaw's ``sessions_list / sessions_history / sessions_send /
sessions_spawn / sessions_yield / session_status`` tool family.

Read-only tools (``sessions_status``, ``sessions_list``, ``sessions_history``)
are safe to expose without approval gating.

Mutating tools (``sessions_send``, ``sessions_spawn``, ``sessions_yield``)
MUST be wrapped with :func:`oxenclaw.approvals.tool_wrap.gated_tool` when an
``ApprovalManager`` is available.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.pi import UserMessage
from oxenclaw.pi.session import AgentSession, CreateAgentSessionOptions, SessionManager
from oxenclaw.tools_pkg._desc import hermes_desc

if TYPE_CHECKING:
    from oxenclaw.approvals.manager import ApprovalManager


# ---------------------------------------------------------------------------
# Arg models (extra="forbid" — schema validated before tool executes)
# ---------------------------------------------------------------------------


class _StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field("default", min_length=1)
    session_key: str = Field(..., min_length=1)


class _ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = Field(None, min_length=1)


class _HistoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = Field(None, min_length=1)
    session_key: str = Field(..., min_length=1)
    limit: int = Field(20, ge=1)


class _SendArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = Field(None, min_length=1)
    session_key: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class _SpawnArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = Field(None, min_length=1)
    parent_session_key: str = Field(..., min_length=1)
    child_session_key: str = Field(..., min_length=1)
    copy_compactions: bool = True


class _YieldArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = Field(None, min_length=1)
    session_key: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_session_id(key: str) -> str:
    """session_key is just the session id in our store."""
    return key.strip()


def _last_assistant_preview(session: AgentSession, max_chars: int = 200) -> str | None:
    for msg in reversed(session.messages):
        if getattr(msg, "role", None) == "assistant":
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for block in content:
                    if getattr(block, "type", None) == "text":
                        return block.text[:max_chars]
            elif isinstance(content, str):
                return content[:max_chars]
    return None


def _has_plan(session: AgentSession) -> bool:
    """Return True when any message contains a <plan> block."""
    for msg in session.messages:
        content = getattr(msg, "content", None)
        if isinstance(content, str) and "<plan>" in content:
            return True
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", "") or ""
                if "<plan>" in text:
                    return True
    return False


# ---------------------------------------------------------------------------
# Tool factories
# ---------------------------------------------------------------------------


def sessions_status_tool(sm: SessionManager) -> Tool:
    """Read-only: return metadata for a single session."""

    async def _h(args: _StatusArgs) -> str:
        sid = _resolve_session_id(args.session_key)
        session = await sm.get(sid)
        if session is None:
            return json.dumps({"error": f"session not found: {sid}"})
        result: dict[str, Any] = {
            "id": session.id,
            "title": session.title,
            "agent_id": session.agent_id,
            "model_id": session.model_id,
            "message_count": len(session.messages),
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "has_plan": _has_plan(session),
            "last_assistant_preview": _last_assistant_preview(session),
        }
        return json.dumps(result)

    return FunctionTool(
        name="sessions_status",
        description=hermes_desc(
            "Return metadata for one session (title, counts, last_assistant_preview, has_plan).",
            when_use=[
                "you have a session_key and need its current status",
            ],
            when_skip=[
                "you want to enumerate sessions (use sessions_list)",
                "you need full transcript (use sessions_history)",
            ],
            alternatives={
                "sessions_list": "list across sessions",
                "sessions_history": "full message tail",
            },
        ),
        input_model=_StatusArgs,
        handler=_h,
    )


def sessions_list_tool(sm: SessionManager) -> Tool:
    """Read-only: list all sessions, optionally filtered by agent_id."""

    async def _h(args: _ListArgs) -> str:
        entries = await sm.list(agent_id=args.agent_id)
        rows = [
            {
                "id": e.id,
                "title": e.title,
                "agent_id": e.agent_id,
                "model_id": e.model_id,
                "message_count": e.message_count,
                "created_at": e.created_at,
                "updated_at": e.updated_at,
            }
            for e in entries
        ]
        return json.dumps(rows)

    return FunctionTool(
        name="sessions_list",
        description=hermes_desc(
            "List sessions (optionally filtered by agent_id) as a JSON array of summaries.",
            when_use=[
                "the user wants to browse / pick a session by id",
                "you need to discover the session_key for a follow-up tool",
            ],
            when_skip=[
                "you already have the session_key (use sessions_status)",
            ],
            alternatives={"sessions_status": "single-session metadata"},
        ),
        input_model=_ListArgs,
        handler=_h,
    )


def sessions_history_tool(sm: SessionManager) -> Tool:
    """Read-only: return the last N messages of a session."""

    async def _h(args: _HistoryArgs) -> str:
        sid = _resolve_session_id(args.session_key)
        session = await sm.get(sid)
        if session is None:
            return json.dumps({"error": f"session not found: {sid}"})
        tail = session.messages[-args.limit :]
        rows = []
        for msg in tail:
            role = getattr(msg, "role", "unknown")
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                preview = content[:200]
            elif isinstance(content, list):
                texts = []
                for block in content:
                    t = getattr(block, "text", None)
                    if t:
                        texts.append(t)
                preview = " ".join(texts)[:200]
            else:
                preview = ""
            rows.append({"role": role, "preview": preview})
        return json.dumps({"session_key": sid, "messages": rows, "total": len(session.messages)})

    return FunctionTool(
        name="sessions_history",
        description=hermes_desc(
            "Return the last `limit` messages (default 20) from a session as {role, preview} rows.",
            when_use=[
                "you need to read what was actually said in a prior session",
            ],
            when_skip=[
                "you only need session metadata (use sessions_status)",
                "you want substring search (use session_logs action=grep)",
            ],
            alternatives={"session_logs": "grep across sessions"},
        ),
        input_model=_HistoryArgs,
        handler=_h,
    )


def sessions_send_tool(sm: SessionManager) -> Tool:
    """Mutating: append a synthetic user message to a session.

    NOTE: this does NOT trigger a new agent run. The message is appended to
    the session's history so the operator can review it and re-engage from
    the dashboard. Requires approval when an ApprovalManager is wired in.
    """

    async def _h(args: _SendArgs) -> str:
        sid = _resolve_session_id(args.session_key)
        session = await sm.get(sid)
        if session is None:
            return json.dumps({"error": f"session not found: {sid}"})
        msg = UserMessage(content=args.text)
        session.messages.append(msg)
        await sm.save(session)
        return json.dumps(
            {
                "ok": True,
                "session_key": sid,
                "message_count": len(session.messages),
                "appended": {"role": "user", "content": args.text},
                "note": (
                    "Message appended to history only. "
                    "No agent run was started. "
                    "Re-engage from the dashboard to continue the conversation."
                ),
            }
        )

    return FunctionTool(
        name="sessions_send",
        description=hermes_desc(
            "Append a synthetic user message to a session's history.",
            when_use=[
                "you want a message recorded in a session for later replay",
            ],
            when_skip=[
                "you expect the agent to act on it now — this does NOT run",
                "the user is in a live chat (just reply normally)",
            ],
            alternatives={"message": "real channel send to a user"},
            notes=("Does NOT trigger an agent run. Mutating — approval-gated when available."),
        ),
        input_model=_SendArgs,
        handler=_h,
    )


def sessions_spawn_tool(sm: SessionManager) -> Tool:
    """Mutating: create an empty child session linked to a parent."""

    async def _h(args: _SpawnArgs) -> str:
        parent_sid = _resolve_session_id(args.parent_session_key)
        child_sid = _resolve_session_id(args.child_session_key)

        # Verify parent exists.
        parent = await sm.get(parent_sid)
        if parent is None:
            return json.dumps({"error": f"parent session not found: {parent_sid}"})

        # Check child doesn't already exist.
        existing = await sm.get(child_sid)
        if existing is not None:
            return json.dumps({"error": f"child session already exists: {child_sid}"})

        # Create child session using the same agent_id as parent.
        opts = CreateAgentSessionOptions(
            agent_id=args.agent_id or parent.agent_id,
            model_id=parent.model_id,
            title=f"child of {parent_sid}",
            metadata={
                "_meta": {
                    "kind": "spawn",
                    "parent_session_key": parent_sid,
                    "created_at": time.time(),
                }
            },
        )
        child = await sm.create(opts)

        # If copy_compactions, copy parent compactions into child.
        if args.copy_compactions and parent.compactions:
            child.compactions = list(parent.compactions)
            await sm.save(child)

        return json.dumps(
            {
                "ok": True,
                "child_session_key": child.id,
                "parent_session_key": parent_sid,
                "agent_id": child.agent_id,
            }
        )

    return FunctionTool(
        name="sessions_spawn",
        description=hermes_desc(
            "Create an empty child session linked to a parent (lineage in "
            "_meta.parent_session_key).",
            when_use=[
                "you need a fresh transcript that inherits parent's compactions",
                "modeling a fork-style sub-conversation",
            ],
            when_skip=[
                "a single tool call would do (use subagents for one-shot)",
                "you don't need separate session storage",
            ],
            alternatives={"subagents": "one-shot isolated child agent"},
            notes="Mutating — approval-gated when available.",
        ),
        input_model=_SpawnArgs,
        handler=_h,
    )


def sessions_yield_tool(sm: SessionManager) -> Tool:
    """Mutating: append a yield-marker message to a session."""

    async def _h(args: _YieldArgs) -> str:
        sid = _resolve_session_id(args.session_key)
        session = await sm.get(sid)
        if session is None:
            return json.dumps({"error": f"session not found: {sid}"})

        # We store the yield as a plain dict appended to session.messages.
        # This avoids needing a custom message type while remaining JSON-
        # serialisable and detectable by callers checking role + meta.kind.
        marker: dict[str, Any] = {
            "role": "assistant",
            "content": f"<yield>{args.summary}</yield>",
            "meta": {"kind": "yield", "summary": args.summary},
        }
        session.messages.append(marker)  # type: ignore[arg-type]
        await sm.save(session)
        return json.dumps({"ok": True, "session_key": sid, "summary": args.summary})

    return FunctionTool(
        name="sessions_yield",
        description=hermes_desc(
            "Append a <yield>summary</yield> marker to a session so a "
            "parent agent can detect completion.",
            when_use=[
                "you're a child session signaling 'done — here is my summary'",
            ],
            when_skip=[
                "you want to abort the current turn (use the runtime yield tool)",
                "you're a parent — read children with sessions_history",
            ],
            alternatives={
                "sessions_history": "parent reading child output",
                "sessions_yield (runtime)": "abort current run loop",
            },
            notes="Mutating — approval-gated when available.",
        ),
        input_model=_YieldArgs,
        handler=_h,
    )


# ---------------------------------------------------------------------------
# Convenience builder
# ---------------------------------------------------------------------------


def build_session_tools(
    sm: SessionManager,
    *,
    approval_manager: ApprovalManager | None = None,
) -> list[Tool]:
    """Build all six session tools.

    Read-only tools (``sessions_status``, ``sessions_list``,
    ``sessions_history``) are always returned ungated.

    Mutating tools (``sessions_send``, ``sessions_spawn``,
    ``sessions_yield``) are wrapped with
    :func:`~oxenclaw.approvals.tool_wrap.gated_tool` when
    *approval_manager* is supplied.
    """
    readonly: list[Tool] = [
        sessions_status_tool(sm),
        sessions_list_tool(sm),
        sessions_history_tool(sm),
    ]
    mutating_raw: list[Tool] = [
        sessions_send_tool(sm),
        sessions_spawn_tool(sm),
        sessions_yield_tool(sm),
    ]

    if approval_manager is not None:
        from oxenclaw.approvals.tool_wrap import gated_tool

        mutating: list[Tool] = [gated_tool(t, manager=approval_manager) for t in mutating_raw]
    else:
        mutating = mutating_raw

    return [*readonly, *mutating]


__all__ = [
    "build_session_tools",
    "sessions_history_tool",
    "sessions_list_tool",
    "sessions_send_tool",
    "sessions_spawn_tool",
    "sessions_status_tool",
    "sessions_yield_tool",
]
