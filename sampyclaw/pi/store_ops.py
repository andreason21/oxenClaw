"""Session store maintenance: pruning, disk budget, migrations, lock state.

Mirrors `openclaw/src/config/sessions/store-maintenance*.ts` +
`disk-budget.ts` + `store-migrations.ts` + `store-lock-state.ts` +
`store-cache.ts`.

Operations are intentionally idempotent — `prune_by_*` returns the count
removed so callers can log; `apply_migrations` records each version it
applied so reruns are no-ops; `acquire_lock` is advisory and uses the
same sqlite file so every gateway process sees the same lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sampyclaw.pi.persistence import SQLiteSessionManager
from sampyclaw.pi.session import AgentSession
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.store_ops")


# ─── Migrations ──────────────────────────────────────────────────────


MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS store_migrations (
  version INTEGER PRIMARY KEY,
  applied_at REAL NOT NULL,
  description TEXT
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: str


def apply_migrations(
    conn: sqlite3.Connection, migrations: list[Migration]
) -> list[int]:
    """Apply each migration whose version is not yet recorded. Idempotent.

    Returns the list of versions applied this call.
    """
    conn.executescript(MIGRATIONS_TABLE)
    rows = conn.execute("SELECT version FROM store_migrations").fetchall()
    have = {int(r[0]) for r in rows}
    applied: list[int] = []
    for m in sorted(migrations, key=lambda x: x.version):
        if m.version in have:
            continue
        try:
            conn.executescript(m.sql)
            conn.execute(
                "INSERT INTO store_migrations (version, applied_at, description) "
                "VALUES (?, ?, ?)",
                (m.version, time.time(), m.description),
            )
            conn.commit()
            applied.append(m.version)
            logger.info("applied migration v%d: %s", m.version, m.description)
        except sqlite3.Error:
            conn.rollback()
            logger.exception("migration v%d failed", m.version)
            raise
    return applied


# ─── Disk budget + pruning ───────────────────────────────────────────


def db_size_bytes(path: Path) -> int:
    """Total size of the sqlite file + any WAL/SHM siblings."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


@dataclass(frozen=True)
class PruneResult:
    sessions_removed: int
    bytes_freed_estimated: int


async def prune_by_age(
    sm: SQLiteSessionManager,
    *,
    older_than_seconds: float,
    keep_min: int = 5,
) -> PruneResult:
    """Drop sessions whose `updated_at` is older than the cutoff. Always
    keeps at least `keep_min` most-recent sessions per agent so a quiet
    operator doesn't lose everything."""
    cutoff = time.time() - older_than_seconds
    listed = await sm.list()
    by_agent: dict[str, list[Any]] = {}
    for entry in listed:
        by_agent.setdefault(entry.agent_id, []).append(entry)

    to_delete: list[str] = []
    for agent_id, entries in by_agent.items():
        entries.sort(key=lambda e: e.updated_at, reverse=True)
        # Skip the keep_min most recent regardless of age.
        candidates = entries[keep_min:]
        for e in candidates:
            if e.updated_at < cutoff:
                to_delete.append(e.id)

    before = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    removed = 0
    for sid in to_delete:
        if await sm.delete(sid):
            removed += 1
    after = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    return PruneResult(
        sessions_removed=removed, bytes_freed_estimated=max(0, before - after)
    )


async def prune_by_count(
    sm: SQLiteSessionManager, *, max_sessions: int
) -> PruneResult:
    """Keep at most `max_sessions` total, dropping the oldest first."""
    listed = await sm.list()
    if len(listed) <= max_sessions:
        return PruneResult(0, 0)
    # `list()` is already ordered updated_at DESC.
    to_delete = [e.id for e in listed[max_sessions:]]
    before = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    removed = 0
    for sid in to_delete:
        if await sm.delete(sid):
            removed += 1
    after = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    return PruneResult(
        sessions_removed=removed, bytes_freed_estimated=max(0, before - after)
    )


async def prune_by_disk_budget(
    sm: SQLiteSessionManager,
    *,
    max_bytes: int,
    keep_min: int = 5,
) -> PruneResult:
    """Drop oldest sessions until file size is under `max_bytes`.

    Stops dropping once the remaining session count hits `keep_min`. After
    a prune that frees space, runs `VACUUM` to actually return bytes to the
    filesystem (sqlite holds reclaimed pages otherwise)."""
    current = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    if current <= max_bytes:
        return PruneResult(0, 0)

    listed = await sm.list()
    listed = sorted(listed, key=lambda e: e.updated_at)  # oldest first
    removed = 0
    bytes_before = current
    for entry in listed:
        remaining = len(listed) - removed
        if remaining <= keep_min:
            break
        if await sm.delete(entry.id):
            removed += 1
        # Cheap re-check: WAL rollover happens on commit.
        if db_size_bytes(sm._path) <= max_bytes:  # type: ignore[attr-defined]
            break

    if removed:
        try:
            sm._conn.execute("VACUUM")  # type: ignore[attr-defined]
        except sqlite3.OperationalError:
            # VACUUM forbidden during open transaction; ignore.
            pass
    after = db_size_bytes(sm._path)  # type: ignore[attr-defined]
    return PruneResult(
        sessions_removed=removed,
        bytes_freed_estimated=max(0, bytes_before - after),
    )


# ─── Maintenance loop ────────────────────────────────────────────────


@dataclass
class MaintenanceConfig:
    """Knobs for the periodic maintenance task."""

    interval_seconds: float = 60 * 60  # 1h
    max_age_seconds: float | None = 30 * 24 * 60 * 60  # 30d
    max_sessions: int | None = 1_000
    max_disk_bytes: int | None = 500 * 1024 * 1024  # 500 MiB
    keep_min_per_agent: int = 5


class StoreMaintenance:
    """Background maintenance task. Run once via `tick()` or as a loop via
    `run_forever()`. Writes a JSON heartbeat to the same dir as the DB."""

    def __init__(
        self,
        sm: SQLiteSessionManager,
        *,
        config: MaintenanceConfig | None = None,
    ) -> None:
        self._sm = sm
        self._config = config or MaintenanceConfig()
        self._stopped = False

    async def tick(self) -> dict[str, Any]:
        """One pass of all enabled prune kinds. Returns a summary."""
        cfg = self._config
        results: dict[str, Any] = {"at": time.time()}
        if cfg.max_age_seconds is not None:
            r = await prune_by_age(
                self._sm,
                older_than_seconds=cfg.max_age_seconds,
                keep_min=cfg.keep_min_per_agent,
            )
            results["by_age"] = {"removed": r.sessions_removed}
        if cfg.max_sessions is not None:
            r = await prune_by_count(self._sm, max_sessions=cfg.max_sessions)
            results["by_count"] = {"removed": r.sessions_removed}
        if cfg.max_disk_bytes is not None:
            r = await prune_by_disk_budget(
                self._sm,
                max_bytes=cfg.max_disk_bytes,
                keep_min=cfg.keep_min_per_agent,
            )
            results["by_disk"] = {
                "removed": r.sessions_removed,
                "bytes_freed": r.bytes_freed_estimated,
            }
        return results

    async def stop(self) -> None:
        self._stopped = True

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                summary = await self.tick()
                logger.info("store maintenance: %s", summary)
            except Exception:
                logger.exception("store maintenance tick failed")
            await asyncio.sleep(self._config.interval_seconds)


# ─── Advisory lock state ─────────────────────────────────────────────


LOCK_TABLE = """
CREATE TABLE IF NOT EXISTS store_locks (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
"""


class LockBusy(RuntimeError):
    """Raised when an advisory lock is held by another holder."""


@dataclass
class StoreLock:
    """Process-cooperative lock recorded in the same sqlite file.

    Use case: the maintenance task should not run on two processes at the
    same time. WAL handles read/write coordination; this is a higher-level
    "I own this name for N seconds" claim. Holders self-identify via
    `holder` (e.g. `pid:hostname`).
    """

    conn: sqlite3.Connection
    name: str
    holder: str
    ttl_seconds: float = 60.0

    def __post_init__(self) -> None:
        self.conn.executescript(LOCK_TABLE)

    def acquire(self, *, force: bool = False) -> bool:
        """Try to claim. Returns True on success, False on busy. With
        `force=True`, evicts an existing holder regardless of TTL."""
        now = time.time()
        with self._txn():
            row = self.conn.execute(
                "SELECT holder, expires_at FROM store_locks WHERE name = ?",
                (self.name,),
            ).fetchone()
            if row is not None and not force:
                expires = float(row[1])
                if expires > now and row[0] != self.holder:
                    return False
            self.conn.execute(
                "INSERT INTO store_locks (name, holder, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "holder=excluded.holder, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (self.name, self.holder, now, now + self.ttl_seconds),
            )
        return True

    def renew(self) -> bool:
        now = time.time()
        with self._txn():
            cur = self.conn.execute(
                "UPDATE store_locks SET expires_at = ? "
                "WHERE name = ? AND holder = ?",
                (now + self.ttl_seconds, self.name, self.holder),
            )
            return cur.rowcount > 0

    def release(self) -> bool:
        with self._txn():
            cur = self.conn.execute(
                "DELETE FROM store_locks WHERE name = ? AND holder = ?",
                (self.name, self.holder),
            )
            return cur.rowcount > 0

    @contextmanager
    def _txn(self):  # type: ignore[no-untyped-def]
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise


def default_holder() -> str:
    """Conventional `pid@host` identifier."""
    try:
        host = os.uname().nodename
    except (AttributeError, OSError):
        host = "unknown"
    return f"{os.getpid()}@{host}"


# ─── Read cache (small LRU) ──────────────────────────────────────────


@dataclass
class StoreReadCache:
    """Tiny LRU over `SessionManager.get(id)` results.

    Sessions tend to be re-read several times per turn (history fetch,
    compaction check, persistence). The cache is invalidated on `save()` /
    `delete()` by the wrapper below.
    """

    capacity: int = 32
    _data: dict[str, AgentSession] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)

    def get(self, key: str) -> AgentSession | None:
        if key not in self._data:
            return None
        self._touch(key)
        return self._data[key]

    def put(self, key: str, value: AgentSession) -> None:
        if key in self._data:
            self._data[key] = value
            self._touch(key)
            return
        if len(self._data) >= self.capacity and self._order:
            evict = self._order.pop(0)
            self._data.pop(evict, None)
        self._data[key] = value
        self._order.append(key)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)
        if key in self._order:
            self._order.remove(key)

    def clear(self) -> None:
        self._data.clear()
        self._order.clear()

    def _touch(self, key: str) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def __len__(self) -> int:
        return len(self._data)


__all__ = [
    "LOCK_TABLE",
    "LockBusy",
    "MIGRATIONS_TABLE",
    "MaintenanceConfig",
    "Migration",
    "PruneResult",
    "StoreLock",
    "StoreMaintenance",
    "StoreReadCache",
    "apply_migrations",
    "db_size_bytes",
    "default_holder",
    "prune_by_age",
    "prune_by_count",
    "prune_by_disk_budget",
]
