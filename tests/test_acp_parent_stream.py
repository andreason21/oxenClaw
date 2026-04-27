"""Tests for the ACP parent-stream relay scaffold."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from oxenclaw.agents.acp_parent_stream import (
    AcpParentStreamRelay,
    STREAM_SNIPPET_MAX_CHARS,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_start_emits_start_notice_and_logs_to_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "child.acp-stream.jsonl"
    surfaced: list[tuple[str, str]] = []

    async def surface(text: str, key: str) -> None:
        surfaced.append((text, key))

    relay = AcpParentStreamRelay(
        run_id="run-1",
        parent_session_key="parent:1",
        child_session_key="child:1",
        agent_id="claude-code",
        log_path=log_path,
        surface=surface,
    )
    try:
        await relay.start()
    finally:
        await relay.dispose()

    # surface saw exactly one start notice
    assert len(surfaced) == 1
    text, key = surfaced[0]
    assert "Started" in text
    assert "child:1" in text
    assert key == "acp-spawn:run-1:start"

    # JSONL log holds at least the start system_event + the end entry
    entries = _read_jsonl(log_path)
    kinds = [e["kind"] for e in entries]
    assert "system_event" in kinds
    assert kinds[-1] == "end"
    start_event = next(e for e in entries if e["kind"] == "system_event")
    assert start_event["context_key"] == "acp-spawn:run-1:start"
    assert start_event["run_id"] == "run-1"


async def test_feed_progress_coalesces_into_single_emit(tmp_path: Path) -> None:
    surfaced: list[tuple[str, str]] = []

    async def surface(text: str, key: str) -> None:
        surfaced.append((text, key))

    relay = AcpParentStreamRelay(
        run_id="run-2",
        parent_session_key="p",
        child_session_key="c",
        agent_id="codex",
        surface=surface,
        emit_start_notice=False,
        stream_flush_seconds=0.05,
    )
    await relay.start()
    try:
        await relay.feed_progress("hello ")
        await relay.feed_progress("world ")
        await relay.feed_progress("from codex")
        # Wait long enough for the flush task to fire.
        await asyncio.sleep(0.15)
    finally:
        await relay.dispose()

    progress = [s for s in surfaced if s[1].endswith(":progress")]
    assert len(progress) == 1
    assert "codex: hello world from codex" in progress[0][0]


async def test_progress_snippet_truncated_at_cap(tmp_path: Path) -> None:
    surfaced: list[str] = []

    async def surface(text: str, _key: str) -> None:
        surfaced.append(text)

    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="gemini",
        surface=surface,
        emit_start_notice=False,
        stream_flush_seconds=0.01,
    )
    await relay.start()
    try:
        await relay.feed_progress("x" * (STREAM_SNIPPET_MAX_CHARS + 500))
        await asyncio.sleep(0.05)
    finally:
        await relay.dispose()

    progress = [s for s in surfaced if "gemini:" in s]
    assert len(progress) == 1
    body = progress[0].removeprefix("gemini: ")
    # Truncated body ends in the ellipsis sentinel and is at most cap chars
    assert len(body) <= STREAM_SNIPPET_MAX_CHARS
    assert body.endswith("…")


async def test_stall_watchdog_fires_after_idle_window(tmp_path: Path) -> None:
    """With a synthetic clock, simulate 60s of idle and confirm
    the stall notice fires exactly once."""
    surfaced: list[tuple[str, str]] = []

    async def surface(text: str, key: str) -> None:
        surfaced.append((text, key))

    # Fake clock advances on demand.
    fake_now = {"t": 0.0}

    def clock() -> float:
        return fake_now["t"]

    poll_count = {"n": 0}

    async def fake_sleep(seconds: float) -> None:
        # Each "sleep" advances the clock by the requested duration
        # and yields control so other tasks can observe state.
        fake_now["t"] += seconds
        poll_count["n"] += 1
        # Real-time yield so the watcher loop progresses.
        await asyncio.sleep(0)

    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="claude",
        surface=surface,
        emit_start_notice=False,
        no_output_notice_seconds=60.0,
        no_output_poll_seconds=15.0,
        max_relay_lifetime_seconds=10_000.0,
        clock=clock,
        sleep=fake_sleep,
    )
    await relay.start()
    try:
        # Yield repeatedly so the stall watcher's loop runs through
        # at least 5 polls (5 * 15s = 75s, past the 60s threshold).
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        await relay.dispose()

    stalls = [s for s in surfaced if s[1].endswith(":stall")]
    assert len(stalls) == 1
    assert "no output for 60s" in stalls[0][0]


async def test_feed_progress_resets_stall_state(tmp_path: Path) -> None:
    surfaced: list[tuple[str, str]] = []

    async def surface(text: str, key: str) -> None:
        surfaced.append((text, key))

    fake_now = {"t": 0.0}

    def clock() -> float:
        return fake_now["t"]

    async def fake_sleep(seconds: float) -> None:
        fake_now["t"] += seconds
        await asyncio.sleep(0)

    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="claude",
        surface=surface,
        emit_start_notice=False,
        no_output_notice_seconds=60.0,
        no_output_poll_seconds=15.0,
        max_relay_lifetime_seconds=10_000.0,
        clock=clock,
        sleep=fake_sleep,
    )
    await relay.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        # Now feed progress — should clear the stall flag, but the
        # *first* stall has already been emitted; we won't see a
        # second one until 60s of idle accrue again.
        await relay.feed_progress("late update")
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        await relay.dispose()

    stalls = [s for s in surfaced if s[1].endswith(":stall")]
    # Exactly one stall — re-arm path requires another full idle window.
    assert len(stalls) == 1


async def test_log_file_permissions_are_0o600(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="claude",
        log_path=log_path,
        emit_start_notice=False,
    )
    await relay.start()
    try:
        await relay.feed_progress("anything to force a write")
        await asyncio.sleep(0.1)
    finally:
        await relay.dispose()

    # Mode bits — only owner read+write
    mode = log_path.stat().st_mode & 0o777
    assert mode == 0o600


async def test_dispose_is_idempotent_and_cancels_tasks(tmp_path: Path) -> None:
    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="claude",
        emit_start_notice=False,
        max_relay_lifetime_seconds=3_600.0,
    )
    await relay.start()
    await relay.dispose()
    # Second dispose is a no-op — must not raise.
    await relay.dispose()


async def test_no_log_path_means_no_file(tmp_path: Path) -> None:
    relay = AcpParentStreamRelay(
        run_id="r",
        parent_session_key="p",
        child_session_key="c",
        agent_id="claude",
        log_path=None,
        emit_start_notice=False,
    )
    await relay.start()
    try:
        await relay.feed_progress("nothing to disk")
        await asyncio.sleep(0.05)
    finally:
        await relay.dispose()
    # Nothing was written — directory is empty
    assert list(tmp_path.iterdir()) == []
