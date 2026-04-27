"""Structured logging + correlation context.

Activated by setting `OXENCLAW_LOG_FORMAT=json` (other values fall back
to the existing human-readable formatter). Correlation IDs are stored in
a `contextvars.ContextVar` so async tasks inherit them automatically.

Usage in subsystems::

    from oxenclaw.observability.logging import correlation_scope

    with correlation_scope(rpc_id="abc123", method="chat.send"):
        ...

Every `logger.info(...)` issued inside the `with` block carries those
fields in the JSON output. Plain text mode appends `[k=v ...]` to the
message instead.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

# The single source of truth for "what's the current correlation context".
# Stored as a frozen dict so mutations are by replacement (safe across
# tasks).
_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "oxenclaw_log_context", default={}
)


def get_context() -> dict[str, str]:
    """Snapshot of the current logging context for inspection or copying."""
    return dict(_CONTEXT.get())


def new_correlation_id() -> str:
    """Generate a short opaque id suitable for tagging an RPC / turn / event."""
    return uuid.uuid4().hex[:12]


@contextmanager
def correlation_scope(**fields: str) -> Iterator[None]:
    """Push correlation fields for the duration of the `with` block.

    Existing keys are preserved unless overridden. Nested scopes layer.
    """
    base = _CONTEXT.get()
    merged = {**base, **{k: str(v) for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class _ContextFilter(logging.Filter):
    """Attach the current correlation context to every record as `_ctx`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record._ctx = _CONTEXT.get()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line.

    Reserved fields: `ts`, `level`, `logger`, `message`, `pid`. The
    correlation context is merged in at the top level.
    """

    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "_ctx", {}) or {}
        payload: dict[str, object] = {
            "ts": _iso_now(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": record.process,
        }
        # Merge correlation context (don't let it shadow reserved fields).
        for k, v in ctx.items():
            if k not in payload:
                payload[k] = v
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class HumanFormatterWithContext(logging.Formatter):
    """The existing human-readable line, augmented with `[k=v ...]` suffix."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "_ctx", {}) or {}
        if not ctx:
            return base
        suffix = " ".join(f"{k}={v}" for k, v in ctx.items())
        return f"{base} [{suffix}]"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def configure_logging(
    *,
    level: int | str = logging.INFO,
    fmt: str | None = None,
    stream=None,
) -> None:
    """Idempotent logging setup.

    `fmt` precedence: explicit arg → `OXENCLAW_LOG_FORMAT` env →
    "human". Valid values: "json" or "human".
    """
    chosen = (fmt or os.environ.get("OXENCLAW_LOG_FORMAT") or "human").lower()
    handler = logging.StreamHandler(stream or sys.stderr)
    if chosen == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(HumanFormatterWithContext())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    # Replace handlers — test runs and re-configures in the same process
    # need this to be idempotent.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    root.setLevel(level)

    # Quiet a known-noisy websockets log: every TCP-only port probe (port
    # scanners, half-broken proxies, our own desktop-app reachability
    # probe before it switched to HEAD /healthz) emits a full ERROR
    # traceback for `did not receive a valid HTTP request`. The
    # underlying connection is fine — it was just closed before sending
    # an HTTP request. Treating it at WARNING keeps real errors visible
    # without flooding the gateway log.
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    logging.getLogger("websockets.asyncio.server").setLevel(logging.WARNING)


__all__ = [
    "JsonFormatter",
    "configure_logging",
    "correlation_scope",
    "get_context",
    "new_correlation_id",
]
