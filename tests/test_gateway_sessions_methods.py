"""tests for sessions.compact RPC method."""

from __future__ import annotations

from oxenclaw.gateway.router import Router
from oxenclaw.gateway.sessions_methods import register_sessions_methods
from oxenclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    SystemMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.session import InMemorySessionManager


def _make_router(sm: InMemorySessionManager) -> Router:
    router = Router()
    register_sessions_methods(router, sm)
    return router


async def _seed_messages(sm: InMemorySessionManager, n: int) -> str:
    """Create a session with n alternating user/assistant messages."""
    s = await sm.create(CreateAgentSessionOptions(agent_id="test", title="t"))
    msgs = [SystemMessage(content="system prompt")]
    for i in range(n // 2):
        msgs.append(UserMessage(content=f"user turn {i}"))
        msgs.append(
            AssistantMessage(
                content=[TextContent(text=f"assistant reply {i}")],
                stop_reason="end_turn",
            )
        )
    # If n is odd, add one more user message
    if n % 2 != 0:
        msgs.append(UserMessage(content=f"user turn extra"))
    s.messages = msgs
    await sm.save(s)
    return s.id


async def test_compact_below_threshold_is_noop() -> None:
    """A session with 2 messages and keep_tail_turns=6 should return compacted=False."""
    sm = InMemorySessionManager()
    sid = await _seed_messages(sm, 2)
    router = _make_router(sm)

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.compact",
            "params": {"id": sid, "keep_tail_turns": 6},
        }
    )
    assert resp.error is None
    result = resp.result
    assert result["ok"] is True
    assert result["compacted"] is False
    assert "below threshold" in result["reason"]

    # Session should be unchanged
    s = await sm.get(sid)
    assert s is not None
    assert len(s.compactions) == 0


async def test_compact_summarises_old_turns() -> None:
    """A session with 14 messages and keep_tail_turns=4 should compact."""
    sm = InMemorySessionManager()
    # 14 messages: 1 system + 6 user/assistant pairs + 1 trailing user = 14
    # Build manually for clarity
    s = await sm.create(CreateAgentSessionOptions(agent_id="test", title="big"))
    msgs = [SystemMessage(content="system")]
    for i in range(6):
        msgs.append(UserMessage(content=f"user {i}"))
        msgs.append(
            AssistantMessage(
                content=[TextContent(text=f"assistant {i}")],
                stop_reason="end_turn",
            )
        )
    msgs.append(UserMessage(content="final user"))
    # Total: 1 + 12 + 1 = 14
    assert len(msgs) == 14
    s.messages = msgs
    await sm.save(s)

    router = _make_router(sm)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.compact",
            "params": {"id": s.id, "keep_tail_turns": 4},
        }
    )
    assert resp.error is None
    result = resp.result
    assert result["ok"] is True
    assert result["compacted"] is True
    assert "checkpoint_id" in result
    assert isinstance(result["tokens_before"], int)
    assert isinstance(result["tokens_after"], int)

    # Compaction record should be appended
    updated = await sm.get(s.id)
    assert updated is not None
    assert len(updated.compactions) == 1
    # Message count should have dropped (summary replaces many old messages)
    assert len(updated.messages) < 14
