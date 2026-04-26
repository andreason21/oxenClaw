"""Phase 6: SQLite-backed SessionManager + AuthStorage."""

from __future__ import annotations

import time
from pathlib import Path

from sampyclaw.pi import (
    AssistantMessage,
    CompactionEntry,
    CreateAgentSessionOptions,
    SystemMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from sampyclaw.pi.persistence import SQLiteAuthStorage, SQLiteSessionManager


async def test_create_get_save_round_trip(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "sessions.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="local", title="t1"))
    s.messages = [
        SystemMessage(content="be brief"),
        UserMessage(content="hi"),
        AssistantMessage(
            content=[
                TextContent(text="hello"),
                ToolUseBlock(id="t1", name="echo", input={"x": 1}),
            ],
            stop_reason="tool_use",
        ),
        ToolResultMessage(results=[ToolResultBlock(tool_use_id="t1", content="x=1")]),
    ]
    s.compactions.append(
        CompactionEntry(
            id="c1",
            summary="prior chat",
            replaced_message_indexes=(0, 1),
            created_at=time.time(),
            reason="auto",
            tokens_before=1000,
            tokens_after=200,
        )
    )
    await sm.save(s)

    fetched = await sm.get(s.id)
    assert fetched is not None
    assert fetched.title == "t1"
    assert len(fetched.messages) == 4
    assert isinstance(fetched.messages[2], AssistantMessage)
    tu = next(b for b in fetched.messages[2].content if isinstance(b, ToolUseBlock))
    assert tu.input == {"x": 1}
    assert isinstance(fetched.messages[3], ToolResultMessage)
    assert fetched.compactions[0].id == "c1"
    sm.close()


async def test_list_filters_by_agent_id(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "sessions.db")
    a = await sm.create(CreateAgentSessionOptions(agent_id="local", title="A"))
    b = await sm.create(CreateAgentSessionOptions(agent_id="other", title="B"))
    await sm.save(a)
    await sm.save(b)
    locals_only = await sm.list(agent_id="local")
    assert [r.title for r in locals_only] == ["A"]
    everything = await sm.list()
    assert {r.title for r in everything} == {"A", "B"}
    sm.close()


async def test_delete_removes_session_and_cascades(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    s.messages = [UserMessage(content="hi")]
    await sm.save(s)
    assert await sm.delete(s.id) is True
    assert await sm.get(s.id) is None
    # Re-deleting returns False.
    assert await sm.delete(s.id) is False
    sm.close()


async def test_save_replaces_messages_wholesale(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    s.messages = [UserMessage(content="first")]
    await sm.save(s)
    s.messages = [UserMessage(content="second")]
    await sm.save(s)
    fetched = await sm.get(s.id)
    assert fetched and len(fetched.messages) == 1
    assert fetched.messages[0].content == "second"  # type: ignore[union-attr]
    sm.close()


async def test_auth_storage_crud(tmp_path: Path) -> None:
    auth = SQLiteAuthStorage(tmp_path / "creds.db")
    assert await auth.get("anthropic") is None
    await auth.set("anthropic", "sk-1")
    assert await auth.get("anthropic") == "sk-1"
    # Update.
    await auth.set("anthropic", "sk-2")
    assert await auth.get("anthropic") == "sk-2"
    # List.
    await auth.set("openai", "sk-o")
    listed = await auth.list_providers()
    assert set(listed) == {"anthropic", "openai"}
    # Delete.
    assert await auth.delete("openai") is True
    assert await auth.delete("openai") is False
    auth.close()


async def test_persistence_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    sm1 = SQLiteSessionManager(db)
    s = await sm1.create(CreateAgentSessionOptions(agent_id="x", title="alive"))
    s.messages = [UserMessage(content="persist me")]
    await sm1.save(s)
    sm1.close()

    sm2 = SQLiteSessionManager(db)
    fetched = await sm2.get(s.id)
    assert fetched and fetched.title == "alive"
    assert fetched.messages[0].content == "persist me"  # type: ignore[union-attr]
    sm2.close()
