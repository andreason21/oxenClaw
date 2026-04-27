"""Tests for session_tools.py — six LLM-callable session tools.

Covers:
- sessions_status: returns metadata + last_assistant_preview
- sessions_list: lists all / filtered sessions
- sessions_history: caps at limit
- sessions_send: appends user message, does not start agent
- sessions_spawn: creates child with _meta linking parent
- sessions_yield: appends yield marker with meta.kind == "yield"
"""

from __future__ import annotations

import json

from oxenclaw.pi import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.session import CreateAgentSessionOptions, InMemorySessionManager
from oxenclaw.tools_pkg.session_tools import (
    sessions_history_tool,
    sessions_list_tool,
    sessions_send_tool,
    sessions_spawn_tool,
    sessions_status_tool,
    sessions_yield_tool,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_session(
    sm: InMemorySessionManager,
    *,
    agent_id: str = "test-agent",
    title: str = "Test Session",
    n_messages: int = 0,
) -> str:
    """Create a session and optionally seed N alternating user/assistant messages."""
    s = await sm.create(CreateAgentSessionOptions(agent_id=agent_id, title=title))
    for i in range(n_messages):
        if i % 2 == 0:
            s.messages.append(UserMessage(content=f"user message {i}"))
        else:
            s.messages.append(
                AssistantMessage(
                    content=[TextContent(text=f"assistant reply {i}")],
                    stop_reason="end_turn",
                )
            )
    if n_messages:
        await sm.save(s)
    return s.id


# ---------------------------------------------------------------------------
# sessions_status
# ---------------------------------------------------------------------------


async def test_sessions_status_returns_metadata() -> None:
    """Populate a session file, call the tool, verify message_count + last_assistant_preview."""
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=4)

    tool = sessions_status_tool(sm)
    raw = await tool.execute({"agent_id": "test-agent", "session_key": sid})
    result = json.loads(raw)

    assert result["id"] == sid
    assert result["message_count"] == 4
    # The last message is assistant reply 3 (index 3)
    assert result["last_assistant_preview"] is not None
    assert "assistant reply" in result["last_assistant_preview"]


async def test_sessions_status_missing_session() -> None:
    sm = InMemorySessionManager()
    tool = sessions_status_tool(sm)
    raw = await tool.execute({"agent_id": "a", "session_key": "no-such-id"})
    result = json.loads(raw)
    assert "error" in result


async def test_sessions_status_has_plan_false_by_default() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=2)
    tool = sessions_status_tool(sm)
    raw = await tool.execute({"agent_id": "a", "session_key": sid})
    result = json.loads(raw)
    assert result["has_plan"] is False


async def test_sessions_status_has_plan_true_when_plan_in_content() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=0)
    session = await sm.get(sid)
    assert session is not None
    session.messages.append(
        AssistantMessage(
            content=[TextContent(text="Here is my plan:\n<plan>\n1. step\n</plan>")],
            stop_reason="end_turn",
        )
    )
    await sm.save(session)

    tool = sessions_status_tool(sm)
    raw = await tool.execute({"agent_id": "a", "session_key": sid})
    result = json.loads(raw)
    assert result["has_plan"] is True


# ---------------------------------------------------------------------------
# sessions_list
# ---------------------------------------------------------------------------


async def test_sessions_list_returns_rows() -> None:
    """Two session files → list returns 2."""
    sm = InMemorySessionManager()
    await _make_session(sm, agent_id="ag1", title="Session A")
    await _make_session(sm, agent_id="ag2", title="Session B")

    tool = sessions_list_tool(sm)
    raw = await tool.execute({})
    rows = json.loads(raw)

    assert len(rows) == 2
    titles = {r["title"] for r in rows}
    assert "Session A" in titles
    assert "Session B" in titles


async def test_sessions_list_filters_by_agent_id() -> None:
    sm = InMemorySessionManager()
    await _make_session(sm, agent_id="alpha")
    await _make_session(sm, agent_id="beta")

    tool = sessions_list_tool(sm)
    raw = await tool.execute({"agent_id": "alpha"})
    rows = json.loads(raw)

    assert len(rows) == 1
    assert rows[0]["agent_id"] == "alpha"


async def test_sessions_list_empty_store() -> None:
    sm = InMemorySessionManager()
    tool = sessions_list_tool(sm)
    raw = await tool.execute({})
    rows = json.loads(raw)
    assert rows == []


# ---------------------------------------------------------------------------
# sessions_history
# ---------------------------------------------------------------------------


async def test_sessions_history_caps_at_limit() -> None:
    """30-message session, limit=5 → 5 rows."""
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=30)

    tool = sessions_history_tool(sm)
    raw = await tool.execute({"session_key": sid, "limit": 5})
    result = json.loads(raw)

    assert len(result["messages"]) == 5
    assert result["total"] == 30


async def test_sessions_history_default_limit_20() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=25)

    tool = sessions_history_tool(sm)
    raw = await tool.execute({"session_key": sid})
    result = json.loads(raw)

    assert len(result["messages"]) == 20


async def test_sessions_history_fewer_than_limit() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=3)

    tool = sessions_history_tool(sm)
    raw = await tool.execute({"session_key": sid, "limit": 10})
    result = json.loads(raw)

    assert len(result["messages"]) == 3


async def test_sessions_history_missing_session() -> None:
    sm = InMemorySessionManager()
    tool = sessions_history_tool(sm)
    raw = await tool.execute({"session_key": "ghost"})
    result = json.loads(raw)
    assert "error" in result


async def test_sessions_history_message_roles() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=2)

    tool = sessions_history_tool(sm)
    raw = await tool.execute({"session_key": sid, "limit": 2})
    result = json.loads(raw)

    roles = [m["role"] for m in result["messages"]]
    assert "user" in roles
    assert "assistant" in roles


# ---------------------------------------------------------------------------
# sessions_send
# ---------------------------------------------------------------------------


async def test_sessions_send_appends_user_message() -> None:
    """Start with N messages, send 'hi', file has N+1 with the new role=user entry."""
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=2)

    tool = sessions_send_tool(sm)
    raw = await tool.execute({"session_key": sid, "text": "hi"})
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["message_count"] == 3

    # Verify on the backing store directly.
    session = await sm.get(sid)
    assert session is not None
    assert len(session.messages) == 3
    last = session.messages[-1]
    assert getattr(last, "role", None) == "user"
    assert getattr(last, "content", None) == "hi"


async def test_sessions_send_note_about_no_agent_run() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm)
    tool = sessions_send_tool(sm)
    raw = await tool.execute({"session_key": sid, "text": "hello"})
    result = json.loads(raw)
    assert "note" in result
    # The note must mention the limitation.
    assert "agent" in result["note"].lower() or "dashboard" in result["note"].lower()


async def test_sessions_send_missing_session() -> None:
    sm = InMemorySessionManager()
    tool = sessions_send_tool(sm)
    raw = await tool.execute({"session_key": "nope", "text": "hi"})
    result = json.loads(raw)
    assert "error" in result


# ---------------------------------------------------------------------------
# sessions_spawn
# ---------------------------------------------------------------------------


async def test_sessions_spawn_creates_child_with_meta() -> None:
    """Call spawn, child file exists, meta contains parent_session_key."""
    sm = InMemorySessionManager()
    parent_id = await _make_session(sm, agent_id="parent-agent", title="Parent")
    child_key = "child-session-001"

    tool = sessions_spawn_tool(sm)
    raw = await tool.execute(
        {"parent_session_key": parent_id, "child_session_key": child_key}
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["parent_session_key"] == parent_id

    # The child was created — retrieve it by the returned id.
    child_id = result["child_session_key"]
    child = await sm.get(child_id)
    assert child is not None

    # Meta must contain parent reference.
    meta = child.metadata.get("_meta", {})
    assert meta.get("parent_session_key") == parent_id
    assert meta.get("kind") == "spawn"


async def test_sessions_spawn_missing_parent() -> None:
    sm = InMemorySessionManager()
    tool = sessions_spawn_tool(sm)
    raw = await tool.execute(
        {"parent_session_key": "no-parent", "child_session_key": "child-x"}
    )
    result = json.loads(raw)
    assert "error" in result


async def test_sessions_spawn_existing_child_is_rejected() -> None:
    sm = InMemorySessionManager()
    parent_id = await _make_session(sm)
    # Spawn once — captures the auto-generated child id.
    tool = sessions_spawn_tool(sm)
    raw1 = await tool.execute(
        {"parent_session_key": parent_id, "child_session_key": "child-dup"}
    )
    r1 = json.loads(raw1)
    child_id = r1["child_session_key"]

    # Try to spawn again using the returned child id as the child key.
    raw2 = await tool.execute(
        {"parent_session_key": parent_id, "child_session_key": child_id}
    )
    r2 = json.loads(raw2)
    assert "error" in r2


async def test_sessions_spawn_copy_compactions_false() -> None:
    sm = InMemorySessionManager()
    parent_id = await _make_session(sm)
    tool = sessions_spawn_tool(sm)
    raw = await tool.execute(
        {
            "parent_session_key": parent_id,
            "child_session_key": "child-no-compact",
            "copy_compactions": False,
        }
    )
    result = json.loads(raw)
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# sessions_yield
# ---------------------------------------------------------------------------


async def test_sessions_yield_appends_marker() -> None:
    """yield with summary 'done', history has marker with <yield>done</yield>
    and meta.kind == 'yield'."""
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=2)

    tool = sessions_yield_tool(sm)
    raw = await tool.execute({"session_key": sid, "summary": "done"})
    result = json.loads(raw)

    assert result["ok"] is True
    assert result["summary"] == "done"

    session = await sm.get(sid)
    assert session is not None

    last = session.messages[-1]
    # The marker is stored as a plain dict.
    if isinstance(last, dict):
        assert last["role"] == "assistant"
        assert "<yield>done</yield>" in last["content"]
        assert last["meta"]["kind"] == "yield"
    else:
        # Fallback for typed message objects.
        content = getattr(last, "content", "")
        assert "<yield>done</yield>" in str(content)


async def test_sessions_yield_missing_session() -> None:
    sm = InMemorySessionManager()
    tool = sessions_yield_tool(sm)
    raw = await tool.execute({"session_key": "ghost", "summary": "x"})
    result = json.loads(raw)
    assert "error" in result


async def test_sessions_yield_increments_message_count() -> None:
    sm = InMemorySessionManager()
    sid = await _make_session(sm, n_messages=4)

    tool = sessions_yield_tool(sm)
    await tool.execute({"session_key": sid, "summary": "wrap-up"})

    session = await sm.get(sid)
    assert session is not None
    assert len(session.messages) == 5


# ---------------------------------------------------------------------------
# Gating: build_session_tools with approval_manager
# ---------------------------------------------------------------------------


def test_build_session_tools_readonly_never_gated() -> None:
    from oxenclaw.approvals.manager import ApprovalManager
    from oxenclaw.tools_pkg.session_tools import build_session_tools

    sm = InMemorySessionManager()
    mgr = ApprovalManager()
    tools = build_session_tools(sm, approval_manager=mgr)
    names = {t.name: t for t in tools}

    # Read-only tools must NOT be gated.
    for name in ("sessions_status", "sessions_list", "sessions_history"):
        assert "approval" not in names[name].description.lower(), (
            f"{name} should not be gated"
        )


def test_build_session_tools_mutating_gated_when_manager_present() -> None:
    from oxenclaw.approvals.manager import ApprovalManager
    from oxenclaw.tools_pkg.session_tools import build_session_tools

    sm = InMemorySessionManager()
    mgr = ApprovalManager()
    tools = build_session_tools(sm, approval_manager=mgr)
    names = {t.name: t for t in tools}

    for name in ("sessions_send", "sessions_spawn", "sessions_yield"):
        assert "approval" in names[name].description.lower(), (
            f"{name} should be gated"
        )


def test_build_session_tools_mutating_ungated_without_manager() -> None:
    from oxenclaw.tools_pkg.session_tools import build_session_tools

    sm = InMemorySessionManager()
    tools = build_session_tools(sm)
    names = {t.name: t for t in tools}

    for name in ("sessions_send", "sessions_spawn", "sessions_yield"):
        assert "(requires human approval before execution)" not in names[name].description, (
            f"{name} should not be gated without approval_manager"
        )
