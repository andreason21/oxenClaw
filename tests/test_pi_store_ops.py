"""Phase 11: store maintenance, pruning, locks, cache."""

from __future__ import annotations

import time
from pathlib import Path

from oxenclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.persistence import SQLiteSessionManager
from oxenclaw.pi.store_ops import (
    MaintenanceConfig,
    Migration,
    StoreLock,
    StoreMaintenance,
    StoreReadCache,
    apply_migrations,
    db_size_bytes,
    default_holder,
    prune_by_age,
    prune_by_count,
    prune_by_disk_budget,
)


async def _seed(sm: SQLiteSessionManager, *, n: int, agent_id: str = "a") -> list[str]:
    ids: list[str] = []
    for i in range(n):
        s = await sm.create(CreateAgentSessionOptions(agent_id=agent_id, title=f"s{i}"))
        s.messages = [
            UserMessage(content=f"hi {i} " + "x" * 1000),
            AssistantMessage(
                content=[TextContent(text=f"a{i} " + "y" * 1000)],
                stop_reason="end_turn",
            ),
        ]
        await sm.save(s)
        ids.append(s.id)
    return ids


# ─── migrations ─────────────────────────────────────────────────────


async def test_migrations_apply_once_idempotent(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "m.db")
    migs = [
        Migration(
            version=1,
            description="add metadata index",
            sql="CREATE INDEX IF NOT EXISTS sessions_meta_idx ON sessions(updated_at);",
        ),
        Migration(
            version=2,
            description="noop second migration",
            sql="-- nothing",
        ),
    ]
    applied1 = apply_migrations(sm._conn, migs)
    assert applied1 == [1, 2]
    # Second run is a no-op.
    applied2 = apply_migrations(sm._conn, migs)
    assert applied2 == []
    sm.close()


async def test_migration_failure_rolls_back(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "m.db")
    bad = [Migration(version=1, description="broken", sql="THIS IS NOT SQL;")]
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.Error):
        apply_migrations(sm._conn, bad)
    # The version row must NOT have been recorded.
    rows = sm._conn.execute("SELECT version FROM store_migrations").fetchall()
    assert rows == []
    sm.close()


# ─── prune by age ────────────────────────────────────────────────────


async def test_prune_by_age_respects_keep_min(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "p.db")
    ids = await _seed(sm, n=10)
    # Backdate every session to "old" so age cuts everything; keep_min=3
    # should still leave 3.
    sm._conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id IN (" + ",".join(["?"] * len(ids)) + ")",
        (time.time() - 10_000, *ids),
    )
    sm._conn.commit()
    r = await prune_by_age(sm, older_than_seconds=1, keep_min=3)
    assert r.sessions_removed == 7
    listed = await sm.list()
    assert len(listed) == 3
    sm.close()


# ─── prune by count ─────────────────────────────────────────────────


async def test_prune_by_count_drops_oldest(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "c.db")
    await _seed(sm, n=8)
    r = await prune_by_count(sm, max_sessions=3)
    assert r.sessions_removed == 5
    assert len(await sm.list()) == 3
    sm.close()


async def test_prune_by_count_no_op_when_under_cap(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "c.db")
    await _seed(sm, n=2)
    r = await prune_by_count(sm, max_sessions=10)
    assert r.sessions_removed == 0
    sm.close()


# ─── prune by disk budget ───────────────────────────────────────────


async def test_prune_by_disk_budget_runs_until_under(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "d.db")
    await _seed(sm, n=20)
    pre = db_size_bytes(sm._path)
    # Pick a budget below current size to force prunes.
    target = max(10_000, pre // 2)
    r = await prune_by_disk_budget(sm, max_bytes=target, keep_min=2)
    assert r.sessions_removed > 0
    assert len(await sm.list()) >= 2  # keep_min honoured
    sm.close()


# ─── maintenance loop ───────────────────────────────────────────────


async def test_maintenance_tick_summary(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "mt.db")
    await _seed(sm, n=5)
    mt = StoreMaintenance(
        sm,
        config=MaintenanceConfig(
            interval_seconds=999,
            max_age_seconds=None,
            max_sessions=2,
            max_disk_bytes=None,
            keep_min_per_agent=1,
        ),
    )
    summary = await mt.tick()
    assert summary["by_count"]["removed"] == 3
    sm.close()


# ─── advisory lock ──────────────────────────────────────────────────


def test_lock_acquire_release(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "l.db")
    a = StoreLock(sm._conn, name="maint", holder="A", ttl_seconds=10)
    b = StoreLock(sm._conn, name="maint", holder="B", ttl_seconds=10)
    assert a.acquire() is True
    assert b.acquire() is False  # busy
    assert a.release() is True
    assert b.acquire() is True
    sm.close()


def test_lock_force_evicts(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "l2.db")
    a = StoreLock(sm._conn, name="x", holder="A", ttl_seconds=999)
    b = StoreLock(sm._conn, name="x", holder="B", ttl_seconds=999)
    assert a.acquire() is True
    assert b.acquire() is False
    assert b.acquire(force=True) is True
    sm.close()


def test_lock_renew_only_for_holder(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "l3.db")
    a = StoreLock(sm._conn, name="r", holder="A", ttl_seconds=10)
    b = StoreLock(sm._conn, name="r", holder="B", ttl_seconds=10)
    a.acquire()
    assert a.renew() is True
    assert b.renew() is False  # not the holder
    sm.close()


def test_default_holder_format() -> None:
    h = default_holder()
    assert "@" in h
    pid_part, _ = h.split("@", 1)
    assert pid_part.isdigit()


# ─── read cache ─────────────────────────────────────────────────────


def test_read_cache_lru_eviction() -> None:
    cache = StoreReadCache(capacity=2)
    from oxenclaw.pi.session import AgentSession

    a = AgentSession(id="a")
    b = AgentSession(id="b")
    c = AgentSession(id="c")
    cache.put("a", a)
    cache.put("b", b)
    cache.put("c", c)  # evicts "a"
    assert cache.get("a") is None
    assert cache.get("b") is b
    assert cache.get("c") is c


def test_read_cache_get_promotes_to_recent() -> None:
    cache = StoreReadCache(capacity=2)
    from oxenclaw.pi.session import AgentSession

    cache.put("a", AgentSession(id="a"))
    cache.put("b", AgentSession(id="b"))
    cache.get("a")  # touch -> a is now MRU
    cache.put("c", AgentSession(id="c"))  # should evict "b", not "a"
    assert cache.get("b") is None
    assert cache.get("a") is not None


def test_read_cache_invalidate_drops_entry() -> None:
    cache = StoreReadCache()
    from oxenclaw.pi.session import AgentSession

    cache.put("x", AgentSession(id="x"))
    cache.invalidate("x")
    assert cache.get("x") is None
    assert len(cache) == 0
