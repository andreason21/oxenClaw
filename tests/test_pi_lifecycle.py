"""Phase 12: lifecycle (reset, fork, archive, events)."""

from __future__ import annotations

import time
from pathlib import Path

from sampyclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    SystemMessage,
    TextContent,
    UserMessage,
)
from sampyclaw.pi.lifecycle import (
    ForkOptions,
    LifecycleBus,
    LifecycleEvent,
    ResetPolicy,
    archive_session,
    fork_session,
    reset_session,
    reset_session_messages,
    restore_archive,
)
from sampyclaw.pi.persistence import SQLiteSessionManager
from sampyclaw.pi.session import AgentSession, CompactionEntry


def _seeded_session(*, n_user: int = 4, with_system: bool = True) -> AgentSession:
    s = AgentSession(agent_id="t", title="seed")
    if with_system:
        s.messages.append(SystemMessage(content="be brief"))
    for i in range(n_user):
        s.messages.append(UserMessage(content=f"u{i}"))
        s.messages.append(
            AssistantMessage(content=[TextContent(text=f"a{i}")], stop_reason="end_turn")
        )
    return s


# ─── reset_session_messages ─────────────────────────────────────────


def test_full_reset_keeps_only_system_when_configured() -> None:
    s = _seeded_session(n_user=3)
    dropped = reset_session_messages(
        s, ResetPolicy(full=True, keep_system=True, keep_last_user_turns=0)
    )
    assert dropped == 6  # 3 user + 3 assistant
    assert len(s.messages) == 1
    assert isinstance(s.messages[0], SystemMessage)


def test_full_reset_without_system_preservation() -> None:
    s = _seeded_session(n_user=2)
    dropped = reset_session_messages(s, ResetPolicy(full=True, keep_system=False))
    assert dropped == 5  # everything
    assert s.messages == []


def test_partial_reset_keeps_last_n_user_turns() -> None:
    s = _seeded_session(n_user=5)
    reset_session_messages(s, ResetPolicy(full=True, keep_system=True, keep_last_user_turns=2))
    # Expect: system + last 2 user turns + their assistant pairs.
    roles = [m.role for m in s.messages]
    assert roles[0] == "system"
    user_count = sum(1 for r in roles if r == "user")
    assert user_count == 2
    assert s.messages[-1].role == "assistant"


def test_reset_drops_compactions_when_not_kept() -> None:
    s = _seeded_session(n_user=2)
    s.compactions.append(
        CompactionEntry(
            id="c",
            summary="x",
            replaced_message_indexes=(0,),
            created_at=0.0,
            reason="auto",
            tokens_before=0,
            tokens_after=0,
        )
    )
    reset_session_messages(s, ResetPolicy(keep_compactions=False))
    assert s.compactions == []


# ─── reset_session via SessionManager + bus ─────────────────────────


async def test_reset_via_session_manager_emits_event(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "r.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="t"))
    s.messages = _seeded_session(n_user=3).messages
    await sm.save(s)

    seen: list[tuple[LifecycleEvent, dict]] = []

    async def _on_event(kind, payload):  # type: ignore[no-untyped-def]
        seen.append((kind, payload))

    bus = LifecycleBus()
    bus.subscribe(_on_event)
    out = await reset_session(sm, s.id, bus=bus)
    assert out is not None
    assert len(seen) == 1
    kind, payload = seen[0]
    assert kind is LifecycleEvent.RESET
    assert payload["id"] == s.id
    assert payload["dropped"] > 0
    sm.close()


async def test_reset_returns_none_when_session_missing(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "r2.db")
    out = await reset_session(sm, "nope")
    assert out is None
    sm.close()


# ─── fork_session ───────────────────────────────────────────────────


async def test_fork_full_copy_when_until_index_omitted(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "f.db")
    src = await sm.create(CreateAgentSessionOptions(agent_id="x", title="src"))
    src.messages = _seeded_session(n_user=3).messages
    await sm.save(src)

    fork = await fork_session(sm, src.id)
    assert fork is not None
    assert fork.id != src.id
    assert len(fork.messages) == len(src.messages)
    assert fork.metadata.get("forked_from") == src.id
    assert fork.title and fork.title.startswith("fork of")
    sm.close()


async def test_fork_truncates_at_until_index(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "f2.db")
    src = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    src.messages = _seeded_session(n_user=4).messages  # 1 sys + 8 turn msgs
    await sm.save(src)

    fork = await fork_session(sm, src.id, options=ForkOptions(until_index=2))
    assert fork is not None
    assert len(fork.messages) == 3
    sm.close()


async def test_fork_emits_event(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "f3.db")
    src = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    src.messages = _seeded_session(n_user=2).messages
    await sm.save(src)

    seen: list[tuple] = []

    async def _h(kind, payload):  # type: ignore[no-untyped-def]
        seen.append((kind, payload))

    bus = LifecycleBus()
    bus.subscribe(_h)
    fork = await fork_session(sm, src.id, bus=bus)
    assert fork is not None
    assert seen and seen[0][0] is LifecycleEvent.FORKED
    assert seen[0][1]["new_id"] == fork.id
    sm.close()


# ─── archive + restore ─────────────────────────────────────────────


async def test_archive_writes_gzipped_json_and_deletes(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "a.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="archive me"))
    s.messages = _seeded_session(n_user=2).messages
    await sm.save(s)

    archive_dir = tmp_path / "archives"
    result = await archive_session(sm, s.id, archive_dir=archive_dir)
    assert result is not None
    assert result.archive_path.exists()
    assert result.archive_path.suffix == ".gz"
    assert result.bytes_written > 0
    # Live row gone.
    assert await sm.get(s.id) is None
    sm.close()


async def test_archive_keeps_live_when_delete_after_false(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "a2.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x"))
    s.messages = _seeded_session(n_user=1).messages
    await sm.save(s)
    res = await archive_session(sm, s.id, archive_dir=tmp_path / "ar", delete_after=False)
    assert res is not None
    assert await sm.get(s.id) is not None
    sm.close()


async def test_restore_round_trips(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "ar.db")
    s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="round"))
    s.messages = _seeded_session(n_user=2).messages
    s.compactions.append(
        CompactionEntry(
            id="c1",
            summary="prior",
            replaced_message_indexes=(0, 1),
            created_at=time.time(),
            reason="auto",
            tokens_before=100,
            tokens_after=20,
        )
    )
    await sm.save(s)
    res = await archive_session(sm, s.id, archive_dir=tmp_path / "arch")
    assert res is not None

    restored = await restore_archive(sm, res.archive_path)
    assert restored is not None and restored.id == s.id
    fetched = await sm.get(s.id)
    assert fetched is not None
    assert len(fetched.messages) == len(s.messages)
    assert fetched.compactions[0].id == "c1"
    sm.close()


# ─── lifecycle bus ──────────────────────────────────────────────────


async def test_bus_swallows_subscriber_failures() -> None:
    bus = LifecycleBus()
    fired: list[int] = []

    async def _ok(kind, payload):  # type: ignore[no-untyped-def]
        fired.append(1)

    async def _bad(kind, payload):  # type: ignore[no-untyped-def]
        raise RuntimeError("nope")

    bus.subscribe(_ok)
    bus.subscribe(_bad)
    bus.subscribe(_ok)
    await bus.emit(LifecycleEvent.UPDATED, {"x": 1})
    assert sum(fired) == 2  # both "ok" handlers ran despite "bad" raising


def test_bus_unsubscribe() -> None:
    bus = LifecycleBus()

    async def _h(kind, payload):  # type: ignore[no-untyped-def]
        return None

    bus.subscribe(_h)
    assert bus.unsubscribe(_h) is True
    assert bus.unsubscribe(_h) is False
