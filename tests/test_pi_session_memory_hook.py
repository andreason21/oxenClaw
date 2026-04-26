"""Phase 15: SessionMemoryHook — index transcripts into MemoryStore."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.store import MemoryStore
from oxenclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    InMemorySessionManager,
    SystemMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from oxenclaw.pi.lifecycle import (
    LifecycleBus,
    LifecycleEvent,
    archive_session,
)
from oxenclaw.pi.persistence import SQLiteSessionManager
from oxenclaw.pi.session import AgentSession
from oxenclaw.pi.session_memory_hook import (
    SESSION_SOURCE,
    SessionMemoryHook,
    render_transcript,
    session_memory_path,
)
from tests._memory_stubs import StubEmbeddings


def _seeded_session(*, n_user: int = 5) -> AgentSession:
    s = AgentSession(agent_id="local", title="long talk")
    s.messages.append(SystemMessage(content="be helpful"))
    for i in range(n_user):
        s.messages.append(UserMessage(content=f"question {i}: " + "x" * 80))
        s.messages.append(
            AssistantMessage(
                content=[TextContent(text=f"answer {i}: " + "y" * 80)],
                stop_reason="end_turn",
            )
        )
    return s


def _make_store(tmp_path: Path) -> tuple[MemoryStore, EmbeddingCache]:
    store = MemoryStore(tmp_path / "mem.db")
    embeds = StubEmbeddings()
    store.ensure_schema_meta(embeds.provider_name, embeds.model, embeds.dimensions)
    cache = EmbeddingCache(embeds, store)  # type: ignore[arg-type]
    return store, cache


# ─── render ──────────────────────────────────────────────────────────


def test_render_transcript_includes_roles_and_turns() -> None:
    s = _seeded_session(n_user=2)
    text = render_transcript(s)
    assert "# Session" in text
    assert "## system" in text
    assert "## turn 1 — user" in text
    assert "## turn 1 — assistant" in text
    assert "## turn 2 — user" in text
    assert text.endswith("\n")


def test_render_transcript_renders_tool_blocks() -> None:
    s = AgentSession(agent_id="x", title="tools")
    s.messages = [
        UserMessage(content="run x"),
        AssistantMessage(
            content=[
                TextContent(text="I'll run it"),
                ToolUseBlock(id="t1", name="echo", input={"x": 1}),
            ],
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id="t1", content="ok"),
                ToolResultBlock(tool_use_id="t2", content="bad", is_error=True),
            ]
        ),
    ]
    text = render_transcript(s)
    assert "[tool_use echo" in text
    assert "[tool_result for t1]" in text
    assert "[tool_result (error) for t2]" in text


# ─── index_session ──────────────────────────────────────────────────


async def test_index_short_session_skipped(tmp_path: Path) -> None:
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, min_messages=4)
    s = AgentSession(agent_id="x")
    s.messages = [UserMessage(content="hi")]
    n = await hook.index_session(s)
    assert n == 0
    assert store.count_chunks() == 0


async def test_index_session_writes_chunks(tmp_path: Path) -> None:
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    s = _seeded_session(n_user=6)
    n = await hook.index_session(s)
    assert n >= 1
    files = store.list_files(source=SESSION_SOURCE)
    assert len(files) == 1
    assert files[0].path == session_memory_path(s)
    assert store.count_chunks() == n


async def test_index_session_is_idempotent(tmp_path: Path) -> None:
    """Re-indexing the same session replaces, not duplicates."""
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    s = _seeded_session(n_user=4)
    n1 = await hook.index_session(s)
    n2 = await hook.index_session(s)
    assert n1 == n2
    files = store.list_files(source=SESSION_SOURCE)
    assert len(files) == 1
    assert store.count_chunks() == n1


async def test_remove_session_drops_indexed_chunks(tmp_path: Path) -> None:
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    s = _seeded_session(n_user=4)
    await hook.index_session(s)
    assert store.count_chunks() > 0
    await hook.remove_session(s.id, s.agent_id)
    assert store.count_chunks() == 0


# ─── LifecycleBus integration ───────────────────────────────────────


async def test_attach_indexes_on_archive_event(tmp_path: Path) -> None:
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    sm = SQLiteSessionManager(tmp_path / "sess.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="t"))
    s.messages = _seeded_session(n_user=4).messages
    await sm.save(s)

    bus = LifecycleBus()
    hook.attach(bus, sm, on_archived=True)
    # Manually emit ARCHIVED first (the actual archive_session would also
    # delete the live row; for test simplicity we emit before delete).
    await bus.emit(
        LifecycleEvent.ARCHIVED,
        {"id": s.id, "agent_id": s.agent_id, "archive_path": "x", "bytes": 1, "deleted": False},
    )
    assert store.count_chunks() > 0
    sm.close()


async def test_attach_drops_on_deleted_event(tmp_path: Path) -> None:
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    sm = InMemorySessionManager()
    s = AgentSession(agent_id="x")
    s.messages = _seeded_session(n_user=4).messages
    # Pre-index manually.
    await hook.index_session(s)
    assert store.count_chunks() > 0

    bus = LifecycleBus()
    hook.attach(bus, sm)
    await bus.emit(LifecycleEvent.DELETED, {"id": s.id, "agent_id": s.agent_id})
    assert store.count_chunks() == 0


async def test_full_archive_flow_end_to_end(tmp_path: Path) -> None:
    """archive_session emits ARCHIVED → hook reads back from DB before delete?
    Note: archive_session deletes by default; the hook tries `sm.get(id)` and
    gets None. Verify graceful no-op."""
    store, cache = _make_store(tmp_path)
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)
    sm = SQLiteSessionManager(tmp_path / "sess.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    s.messages = _seeded_session(n_user=4).messages
    await sm.save(s)

    bus = LifecycleBus()
    hook.attach(bus, sm)
    await archive_session(sm, s.id, archive_dir=tmp_path / "ar", bus=bus)
    # Hook saw archived but session was already deleted → no chunks.
    assert store.count_chunks() == 0
    sm.close()
