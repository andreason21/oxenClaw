"""SQLite-backed SessionManager + AuthStorage.

Mirrors the persistence layer that `@mariozechner/pi-coding-agent` provides
via its file/SQLite store. Three tables:

- `sessions(id, agent_id, model_id, title, created_at, updated_at, metadata_json)`
- `messages(session_id, idx, payload_json)`
- `compactions(session_id, idx, entry_json)`
- `credentials(provider, api_key, updated_at)`

Indexes on `sessions(agent_id)` and `messages(session_id, idx)`.

Why SQLite (not JSON-per-session): one file, atomic transactions, fast
list queries when the operator has thousands of sessions, and easy
backup. WAL mode + NORMAL synchronous mirror what the memory store uses
for the same throughput characteristics.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sampyclaw.pi.messages import AgentMessage
from sampyclaw.pi.models import ProviderId
from sampyclaw.pi.session import (
    AgentSession,
    CompactionEntry,
    CreateAgentSessionOptions,
    SessionEntry,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  model_id TEXT,
  title TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_agent_idx ON sessions(agent_id);

CREATE TABLE IF NOT EXISTS messages (
  session_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (session_id, idx),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS compactions (
  session_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  entry_json TEXT NOT NULL,
  PRIMARY KEY (session_id, idx),
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS credentials (
  provider TEXT PRIMARY KEY,
  api_key TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.executescript(SCHEMA)
    return conn


# ─── Message (de)serialization ───────────────────────────────────────


def _serialize_message(msg: AgentMessage) -> str:
    return msg.model_dump_json()


def _deserialize_message(payload: str) -> AgentMessage:
    """Discriminated-union round-trip via pydantic TypeAdapter."""
    from pydantic import TypeAdapter

    return TypeAdapter(AgentMessage).validate_json(payload)


def _serialize_compaction(entry: CompactionEntry) -> str:
    return json.dumps(
        {
            "id": entry.id,
            "summary": entry.summary,
            "replaced_message_indexes": list(entry.replaced_message_indexes),
            "created_at": entry.created_at,
            "reason": entry.reason,
            "tokens_before": entry.tokens_before,
            "tokens_after": entry.tokens_after,
            "original_archive_path": entry.original_archive_path,
        }
    )


def _deserialize_compaction(payload: str) -> CompactionEntry:
    data = json.loads(payload)
    return CompactionEntry(
        id=data["id"],
        summary=data["summary"],
        replaced_message_indexes=tuple(data["replaced_message_indexes"]),
        created_at=data["created_at"],
        reason=data["reason"],
        tokens_before=data["tokens_before"],
        tokens_after=data["tokens_after"],
        original_archive_path=data.get("original_archive_path"),
    )


# ─── SQLiteSessionManager ────────────────────────────────────────────


class SQLiteSessionManager:
    """Persistent SessionManager. Single sqlite file under `path`."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn = _open(self._path)

    @contextmanager
    def _txn(self):  # type: ignore[no-untyped-def]
        try:
            yield self._conn
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    async def create(self, opts: CreateAgentSessionOptions) -> AgentSession:
        s = AgentSession(
            agent_id=opts.agent_id,
            model_id=opts.model_id,
            title=opts.title,
            metadata=dict(opts.metadata or {}),
        )
        with self._txn() as c:
            c.execute(
                "INSERT INTO sessions (id, agent_id, model_id, title, "
                "created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    s.id,
                    s.agent_id,
                    s.model_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    json.dumps(s.metadata, ensure_ascii=False),
                ),
            )
        return s

    async def get(self, session_id: str) -> AgentSession | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        msgs_rows = self._conn.execute(
            "SELECT payload_json FROM messages WHERE session_id = ? ORDER BY idx",
            (session_id,),
        ).fetchall()
        comp_rows = self._conn.execute(
            "SELECT entry_json FROM compactions WHERE session_id = ? ORDER BY idx",
            (session_id,),
        ).fetchall()
        s = AgentSession(
            id=row["id"],
            agent_id=row["agent_id"],
            model_id=row["model_id"],
            title=row["title"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
        s.messages = [_deserialize_message(r["payload_json"]) for r in msgs_rows]
        s.compactions = [_deserialize_compaction(r["entry_json"]) for r in comp_rows]
        return s

    async def list(self, *, agent_id: str | None = None) -> list[SessionEntry]:
        sql = (
            "SELECT s.id, s.agent_id, s.model_id, s.title, "
            "       s.created_at, s.updated_at, "
            "       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n "
            "FROM sessions s"
        )
        args: list[Any] = []
        if agent_id is not None:
            sql += " WHERE s.agent_id = ?"
            args.append(agent_id)
        sql += " ORDER BY s.updated_at DESC"
        rows = self._conn.execute(sql, args).fetchall()
        return [
            SessionEntry(
                id=r["id"],
                title=r["title"],
                agent_id=r["agent_id"],
                model_id=r["model_id"],
                message_count=int(r["n"]),
                created_at=float(r["created_at"]),
                updated_at=float(r["updated_at"]),
            )
            for r in rows
        ]

    async def save(self, session: AgentSession) -> None:
        session.updated_at = time.time()
        with self._txn() as c:
            c.execute(
                "INSERT INTO sessions (id, agent_id, model_id, title, "
                "created_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "agent_id=excluded.agent_id, "
                "model_id=excluded.model_id, "
                "title=excluded.title, "
                "updated_at=excluded.updated_at, "
                "metadata_json=excluded.metadata_json",
                (
                    session.id,
                    session.agent_id,
                    session.model_id,
                    session.title,
                    session.created_at,
                    session.updated_at,
                    json.dumps(session.metadata, ensure_ascii=False),
                ),
            )
            # Replace transcript wholesale; messages list is the source of truth.
            c.execute("DELETE FROM messages WHERE session_id = ?", (session.id,))
            if session.messages:
                c.executemany(
                    "INSERT INTO messages (session_id, idx, payload_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (session.id, i, _serialize_message(m))
                        for i, m in enumerate(session.messages)
                    ],
                )
            c.execute(
                "DELETE FROM compactions WHERE session_id = ?", (session.id,)
            )
            if session.compactions:
                c.executemany(
                    "INSERT INTO compactions (session_id, idx, entry_json) "
                    "VALUES (?, ?, ?)",
                    [
                        (session.id, i, _serialize_compaction(e))
                        for i, e in enumerate(session.compactions)
                    ],
                )

    async def delete(self, session_id: str) -> bool:
        with self._txn() as c:
            cur = c.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cur.rowcount > 0


# ─── SQLiteAuthStorage ───────────────────────────────────────────────


class SQLiteAuthStorage:
    """Persistent AuthStorage backed by the same sqlite file as the
    SessionManager. Pass the same path to share the DB."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._conn = _open(self._path)

    def close(self) -> None:
        self._conn.close()

    async def get(self, provider: ProviderId) -> str | None:
        row = self._conn.execute(
            "SELECT api_key FROM credentials WHERE provider = ?", (provider,)
        ).fetchone()
        return row["api_key"] if row else None

    async def set(self, provider: ProviderId, api_key: str) -> None:
        self._conn.execute(
            "INSERT INTO credentials (provider, api_key, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "api_key=excluded.api_key, updated_at=excluded.updated_at",
            (provider, api_key, time.time()),
        )
        self._conn.commit()

    async def delete(self, provider: ProviderId) -> bool:
        cur = self._conn.execute(
            "DELETE FROM credentials WHERE provider = ?", (provider,)
        )
        self._conn.commit()
        return cur.rowcount > 0

    async def list_providers(self) -> list[ProviderId]:
        rows = self._conn.execute(
            "SELECT provider FROM credentials ORDER BY provider"
        ).fetchall()
        return [r["provider"] for r in rows]


__all__ = ["SQLiteAuthStorage", "SQLiteSessionManager"]
