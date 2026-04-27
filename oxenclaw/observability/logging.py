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
    file_path: str | None = None,
) -> None:
    """Idempotent logging setup.

    `fmt` precedence: explicit arg → `OXENCLAW_LOG_FORMAT` env →
    "human". Valid values: "json" or "human".

    `file_path` precedence: explicit arg → `OXENCLAW_LOG_FILE` env →
    `~/.oxenclaw/logs/gateway.log` (default — created if missing).
    Pass an empty string to opt out entirely. The file handler always
    uses the JSON formatter regardless of stream `fmt` so the on-disk
    log stays grep-able / structured even when the operator views the
    terminal in human-friendly mode. Mirrors openclaw's
    `~/.openclaw/logs/` convention.
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

    # Persistent file handler. The terminal is the only sink today, so
    # an operator who wasn't watching the tab during a failure has no
    # way to recover the traceback. Default to a rotating file under
    # ~/.oxenclaw/logs/ unless the operator opts out.
    resolved_file = file_path
    if resolved_file is None:
        resolved_file = os.environ.get("OXENCLAW_LOG_FILE")
    if resolved_file is None:
        home = os.environ.get("OXENCLAW_HOME")
        base = (
            os.path.expanduser(home) if home
            else os.path.join(os.path.expanduser("~"), ".oxenclaw")
        )
        resolved_file = os.path.join(base, "logs", "gateway.log")
    if resolved_file:
        try:
            from logging.handlers import RotatingFileHandler
            os.makedirs(os.path.dirname(resolved_file), exist_ok=True)
            file_handler = RotatingFileHandler(
                resolved_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(JsonFormatter())
            file_handler.addFilter(_ContextFilter())
            root.addHandler(file_handler)
        except OSError:
            # Disk full / permission denied / read-only fs — keep the
            # stream handler so the gateway still starts. The terminal
            # log alone is enough for the dev-loop case.
            pass

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
