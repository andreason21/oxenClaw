"""Outbound HTTP audit log via aiohttp TraceConfig.

Mirrors openclaw `proxy-capture/runtime.ts` + `store.sqlite.ts`. Every
guarded `aiohttp.ClientSession` can be wired with a `TraceConfig` that
records request/response/exception events into a sqlite WAL store.

Opt-in via env (cost is real — see `should_audit_from_env`):
- `OXENCLAW_AUDIT_OUTBOUND=1`           — enable
- `OXENCLAW_AUDIT_OUTBOUND_BODY=1`      — also persist response body
- `OXENCLAW_AUDIT_OUTBOUND_PATH=path`   — sqlite file (default ~/.oxenclaw/outbound-audit.db)
- `OXENCLAW_AUDIT_OUTBOUND_SAMPLE=0.5`  — sample rate (default 1.0)

Schema: one row per `(request_id, event)`; events = `request`, `response`,
`exception`. Bodies are stored in a separate blob table so the events
table stays cheap to scan.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("net.audit")


SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  event TEXT NOT NULL,
  ts REAL NOT NULL,
  method TEXT,
  url TEXT,
  status INTEGER,
  duration_ms REAL,
  bytes INTEGER,
  error TEXT,
  headers_json TEXT
);
CREATE INDEX IF NOT EXISTS outbound_events_request_idx ON outbound_events(request_id);
CREATE INDEX IF NOT EXISTS outbound_events_ts_idx ON outbound_events(ts);

CREATE TABLE IF NOT EXISTS outbound_bodies (
  request_id TEXT PRIMARY KEY,
  direction TEXT NOT NULL,
  content_type TEXT,
  body BLOB NOT NULL
);
"""


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.executescript(SCHEMA)
    return conn


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = False
    capture_body: bool = False
    sample_rate: float = 1.0
    db_path: Path | None = None
    max_body_bytes: int = 64 * 1024  # 64 KiB — anything larger is truncated


def should_audit_from_env(
    env: dict[str, str] | None = None, *, home: Path | None = None
) -> AuditConfig:
    src = env if env is not None else os.environ
    enabled = src.get("OXENCLAW_AUDIT_OUTBOUND", "").lower() in ("1", "true", "yes")
    if not enabled:
        return AuditConfig()
    body = src.get("OXENCLAW_AUDIT_OUTBOUND_BODY", "").lower() in ("1", "true", "yes")
    try:
        rate = float(src.get("OXENCLAW_AUDIT_OUTBOUND_SAMPLE", "1.0"))
    except ValueError:
        rate = 1.0
    rate = max(0.0, min(1.0, rate))
    raw_path = src.get("OXENCLAW_AUDIT_OUTBOUND_PATH")
    if raw_path:
        db_path = Path(raw_path).expanduser()
    else:
        base = home or Path.home() / ".oxenclaw"
        db_path = base / "outbound-audit.db"
    return AuditConfig(enabled=True, capture_body=body, sample_rate=rate, db_path=db_path)


class OutboundAuditStore:
    """Single-process sqlite-backed audit store."""

    def __init__(self, path: Path, *, max_body_bytes: int = 64 * 1024) -> None:
        self._path = Path(path)
        self._conn = _open(self._path)
        self._max_body = max_body_bytes

    def close(self) -> None:
        self._conn.close()

    @property
    def path(self) -> Path:
        return self._path

    def record_event(
        self,
        *,
        request_id: str,
        event: str,
        method: str | None = None,
        url: str | None = None,
        status: int | None = None,
        duration_ms: float | None = None,
        bytes_count: int | None = None,
        error: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO outbound_events "
            "(request_id, event, ts, method, url, status, duration_ms, "
            " bytes, error, headers_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                event,
                time.time(),
                method,
                url,
                status,
                duration_ms,
                bytes_count,
                error,
                json.dumps(headers, ensure_ascii=False) if headers else None,
            ),
        )
        self._conn.commit()

    def record_body(
        self,
        *,
        request_id: str,
        direction: str,
        body: bytes,
        content_type: str | None,
    ) -> None:
        if len(body) > self._max_body:
            body = body[: self._max_body]
        self._conn.execute(
            "INSERT INTO outbound_bodies (request_id, direction, content_type, body) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET "
            "direction=excluded.direction, "
            "content_type=excluded.content_type, body=excluded.body",
            (request_id, direction, content_type, body),
        )
        self._conn.commit()

    # ─── query helpers ──────────────────────────────────────────────

    def recent(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM outbound_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        cols = [
            c[0] for c in self._conn.execute("SELECT * FROM outbound_events LIMIT 0").description
        ]
        return [dict(zip(cols, r, strict=False)) for r in rows]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM outbound_events").fetchone()
        return int(row[0])


def make_audit_trace_config(
    store: OutboundAuditStore,
    *,
    sample_rate: float = 1.0,
    capture_body: bool = False,
) -> aiohttp.TraceConfig:
    """Build a `TraceConfig` that streams events into `store`.

    `sample_rate` filters at the request level — the same request_id is
    used for the matching response/exception so partial pairs are avoided.
    """
    config = aiohttp.TraceConfig()

    def _sample() -> bool:
        return sample_rate >= 1.0 or random.random() < sample_rate

    async def _on_request_start(session, ctx, params):  # type: ignore[no-untyped-def]
        ctx.audit_id = uuid4().hex
        ctx.audit_started = time.monotonic()
        ctx.audit_sampled = _sample()
        if not ctx.audit_sampled:
            return
        store.record_event(
            request_id=ctx.audit_id,
            event="request",
            method=params.method,
            url=str(params.url),
            headers=dict(params.headers) if params.headers else None,
        )

    async def _on_request_end(session, ctx, params):  # type: ignore[no-untyped-def]
        if not getattr(ctx, "audit_sampled", True):
            return
        duration_ms = (time.monotonic() - ctx.audit_started) * 1000
        store.record_event(
            request_id=ctx.audit_id,
            event="response",
            method=params.method,
            url=str(params.url),
            status=params.response.status,
            duration_ms=duration_ms,
            headers=dict(params.response.headers) if params.response.headers else None,
        )
        if capture_body:
            try:
                body = await params.response.read()
            except Exception as exc:  # pragma: no cover
                logger.debug("audit body capture failed: %s", exc)
                return
            store.record_body(
                request_id=ctx.audit_id,
                direction="response",
                body=body,
                content_type=params.response.headers.get("Content-Type"),
            )

    async def _on_request_exception(session, ctx, params):  # type: ignore[no-untyped-def]
        if not getattr(ctx, "audit_sampled", True):
            return
        duration_ms = (time.monotonic() - ctx.audit_started) * 1000
        store.record_event(
            request_id=ctx.audit_id,
            event="exception",
            method=params.method,
            url=str(params.url),
            duration_ms=duration_ms,
            error=str(params.exception)[:500],
        )

    config.on_request_start.append(_on_request_start)
    config.on_request_end.append(_on_request_end)
    config.on_request_exception.append(_on_request_exception)
    return config


__all__ = [
    "AuditConfig",
    "OutboundAuditStore",
    "make_audit_trace_config",
    "should_audit_from_env",
]
