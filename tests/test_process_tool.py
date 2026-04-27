"""Tests for oxenclaw.tools_pkg.process_tool.

These tests require a real shell (bash). They are skipped automatically when
bash is not available, so CI environments without bash stay green.
"""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="needs bash",
)

from oxenclaw.tools_pkg.process_tool import process_tool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(tool, **kwargs) -> dict:
    raw = await tool.execute(kwargs)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_process_start_emits_pid() -> None:
    """start action returns a pid (8 hex chars) and started_at timestamp."""
    tool = process_tool()
    result = await _call(tool, action="start", command="bash -lc 'echo hello'")
    assert "pid" in result
    assert len(result["pid"]) == 8
    assert "started_at" in result
    assert "command" in result

    # Clean up
    await asyncio.sleep(0.2)
    await _call(tool, action="stop", pid=result["pid"])


async def test_process_read_output_captures_stdout() -> None:
    """After starting 'echo hello', read_output should contain 'hello'."""
    tool = process_tool()
    start = await _call(tool, action="start", command="bash -lc 'echo hello'")
    pid = start["pid"]

    # Give the process time to produce output and the reader task to buffer it.
    await asyncio.sleep(0.3)

    result = await _call(tool, action="read_output", pid=pid, timeout_s=0.5)
    assert "output" in result
    assert "hello" in result["output"]

    # Stop (process may already be done, stop should handle that gracefully)
    stop = await _call(tool, action="stop", pid=pid)
    assert "exit_code" in stop


async def test_process_send_keys_into_cat() -> None:
    """Start cat, send 'foo', read_output should return 'foo'."""
    tool = process_tool()
    start = await _call(tool, action="start", command="cat")
    pid = start["pid"]

    result = await _call(tool, action="send_keys", pid=pid, keys="foo", timeout_s=0.5)
    assert "output" in result
    assert "foo" in result["output"]

    await _call(tool, action="stop", pid=pid)


async def test_process_stop_returns_exit_code() -> None:
    """Stopping 'sleep 30' returns exit_code (non-zero from SIGTERM/SIGKILL)."""
    tool = process_tool()
    start = await _call(tool, action="start", command="sleep 30")
    pid = start["pid"]

    stop = await _call(tool, action="stop", pid=pid)
    assert "exit_code" in stop
    assert stop["pid"] == pid
    # Process was killed, so exit_code should be non-None (typically -15 or -9)
    assert stop["exit_code"] is not None


async def test_process_list_after_start() -> None:
    """list action returns at least one process after a start."""
    tool = process_tool()
    start = await _call(tool, action="start", command="sleep 5")
    pid = start["pid"]

    result = await _call(tool, action="list")
    assert "processes" in result
    pids = [p["pid"] for p in result["processes"]]
    assert pid in pids

    await _call(tool, action="stop", pid=pid)
