"""ACP parent-stream relay scaffold — observability for child runs.

Ports the operator-visible parts of openclaw
`src/agents/acp-spawn-parent-stream.ts` to Python. The TS file does
three jobs: project child progress upward to the parent session,
emit best-effort JSONL audit logs, and trip a stall watchdog when
the child goes quiet for too long. Today we only port the second
and third — the actual "project upward" call lands on whatever
parent-event bus oxenclaw eventually exposes.

Why land this against the current one-shot subprocess path even
though there is no real ACP wire yet:

  - The JSONL audit log is useful right now for any long-running
    CLI invocation — operators get a per-session timeline they can
    `tail -f`.
  - The stall watchdog ("no output for 60s — may be waiting for
    input") is the most operator-asked feature in the openclaw
    parity gap, and works whether the source of "progress" events
    is process stdout chunks or real ACP `session/update`
    notifications.

The relay is `asyncio`-only. Construct, call `start()`, feed it
`feed_progress(text)` whenever output observed, and `dispose()` on
end. Two background tasks (`_stall_watch`, `_lifetime_watch`) are
owned by the instance and cancelled on dispose.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.acp_parent_stream")

# Defaults match openclaw acp-spawn-parent-stream.ts:14-19.
DEFAULT_STREAM_FLUSH_SECONDS = 2.5
DEFAULT_NO_OUTPUT_NOTICE_SECONDS = 60.0
DEFAULT_NO_OUTPUT_POLL_SECONDS = 15.0
DEFAULT_MAX_RELAY_LIFETIME_SECONDS = 6 * 60 * 60.0  # 6 hours
STREAM_BUFFER_MAX_CHARS = 4_000
STREAM_SNIPPET_MAX_CHARS = 220


def _compact_whitespace(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return value[:max_chars]
    return value[: max_chars - 1] + "…"


# Surface callback receives the human-readable progress line + a
# context key (mirrors openclaw's `enqueueSystemEvent` signature).
SurfaceCallback = Callable[[str, str], Awaitable[None]]


@dataclass
class AcpParentStreamRelay:
    """Coalescing relay for child-run progress + audit log.

    Most fields are deliberately set on the instance via
    `dataclass(field=field(default=...))` so they are easy to
    override per-test. Tasks are populated by `start()`.
    """

    run_id: str
    parent_session_key: str
    child_session_key: str
    agent_id: str
    log_path: Path | None = None
    surface: SurfaceCallback | None = None
    stream_flush_seconds: float = DEFAULT_STREAM_FLUSH_SECONDS
    no_output_notice_seconds: float = DEFAULT_NO_OUTPUT_NOTICE_SECONDS
    no_output_poll_seconds: float = DEFAULT_NO_OUTPUT_POLL_SECONDS
    max_relay_lifetime_seconds: float = DEFAULT_MAX_RELAY_LIFETIME_SECONDS
    emit_start_notice: bool = True
    # Allow tests to inject a synthetic clock + sleeper.
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    sleep: Callable[[float], Awaitable[None]] = field(default_factory=lambda: asyncio.sleep)

    # ---- internal state (not user-set) ------------------------------------
    _disposed: bool = field(default=False, init=False)
    _pending_text: str = field(default="", init=False)
    _last_progress_at: float = field(default=0.0, init=False)
    _stall_notified: bool = field(default=False, init=False)
    _flush_task: asyncio.Task[None] | None = field(default=None, init=False)
    _stall_task: asyncio.Task[None] | None = field(default=None, init=False)
    _lifetime_task: asyncio.Task[None] | None = field(default=None, init=False)
    _log_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _log_dir_ready: bool = field(default=False, init=False)

    @property
    def _context_prefix(self) -> str:
        return f"acp-spawn:{self.run_id}"

    @property
    def _relay_label(self) -> str:
        compact = _truncate(_compact_whitespace(self.agent_id), 40)
        return compact or "ACP child"

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Begin watchdogs + emit start notice (if enabled)."""
        if self._disposed:
            return
        self._last_progress_at = self.clock()
        self._stall_task = asyncio.create_task(self._stall_watch())
        self._lifetime_task = asyncio.create_task(self._lifetime_watch())
        if self.emit_start_notice:
            await self._emit(
                f"Started {self._relay_label} session {self.child_session_key}. "
                "Streaming progress updates to parent session.",
                f"{self._context_prefix}:start",
            )

    async def feed_progress(self, text: str) -> None:
        """Feed observed child output. Coalesces + schedules a flush."""
        if self._disposed:
            return
        cleaned = text or ""
        if not cleaned:
            return
        self._last_progress_at = self.clock()
        self._stall_notified = False
        self._pending_text = (self._pending_text + cleaned)[-STREAM_BUFFER_MAX_CHARS:]
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def dispose(self, *, reason: str = "end") -> None:
        """Cancel watchdogs, flush any pending text, log lifecycle."""
        if self._disposed:
            return
        self._disposed = True
        for task in (self._flush_task, self._stall_task, self._lifetime_task):
            if task is not None and not task.done():
                task.cancel()
        await self._flush_pending()
        await self._log_event("end", {"reason": reason})

    # ---- internals --------------------------------------------------------

    async def _flush_after_delay(self) -> None:
        try:
            await self.sleep(self.stream_flush_seconds)
        except asyncio.CancelledError:
            return
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        if not self._pending_text:
            return
        snippet = _truncate(_compact_whitespace(self._pending_text), STREAM_SNIPPET_MAX_CHARS)
        self._pending_text = ""
        if not snippet:
            return
        await self._emit(
            f"{self._relay_label}: {snippet}",
            f"{self._context_prefix}:progress",
        )

    async def _stall_watch(self) -> None:
        try:
            while not self._disposed:
                await self.sleep(self.no_output_poll_seconds)
                if self._disposed or self._stall_notified:
                    continue
                idle = self.clock() - self._last_progress_at
                if idle < self.no_output_notice_seconds:
                    continue
                self._stall_notified = True
                await self._emit(
                    f"{self._relay_label} has produced no output for "
                    f"{round(self.no_output_notice_seconds)}s. It may be "
                    "waiting for interactive input.",
                    f"{self._context_prefix}:stall",
                )
        except asyncio.CancelledError:
            return

    async def _lifetime_watch(self) -> None:
        try:
            await self.sleep(self.max_relay_lifetime_seconds)
        except asyncio.CancelledError:
            return
        if self._disposed:
            return
        await self._emit(
            f"{self._relay_label} stream relay timed out after "
            f"{round(self.max_relay_lifetime_seconds)}s without completion.",
            f"{self._context_prefix}:timeout",
        )
        await self.dispose(reason="lifetime_timeout")

    async def _emit(self, text: str, context_key: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        await self._log_event("system_event", {"context_key": context_key, "text": cleaned})
        if self.surface is None:
            return
        try:
            await self.surface(cleaned, context_key)
        except Exception:  # pragma: no cover — surface is operator-supplied
            logger.exception("acp_parent_stream surface callback failed")

    async def _log_event(self, kind: str, fields: dict[str, Any] | None = None) -> None:
        if self.log_path is None:
            return
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "epoch_ms": int(self.clock() * 1000),
            "run_id": self.run_id,
            "parent_session_key": self.parent_session_key,
            "child_session_key": self.child_session_key,
            "agent_id": self.agent_id,
            "kind": kind,
        }
        if fields:
            entry.update(fields)
        try:
            line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover — defensive
            logger.exception("acp_parent_stream log encode failed")
            return
        async with self._log_lock:
            try:
                if not self._log_dir_ready:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    self._log_dir_ready = True
                await asyncio.to_thread(self._append_log_bytes, self.log_path, line)
            except OSError:
                # Best-effort diagnostics — never break relay flow.
                logger.warning("acp_parent_stream log write failed at %s", self.log_path)

    @staticmethod
    def _append_log_bytes(path: Path, line: bytes) -> None:
        # Ensure the file exists with mode 0o600 even if we created it
        # via O_APPEND below.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)


__all__ = [
    "DEFAULT_MAX_RELAY_LIFETIME_SECONDS",
    "DEFAULT_NO_OUTPUT_NOTICE_SECONDS",
    "DEFAULT_NO_OUTPUT_POLL_SECONDS",
    "DEFAULT_STREAM_FLUSH_SECONDS",
    "STREAM_BUFFER_MAX_CHARS",
    "STREAM_SNIPPET_MAX_CHARS",
    "AcpParentStreamRelay",
    "SurfaceCallback",
]
