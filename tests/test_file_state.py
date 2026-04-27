"""Tests for the cross-agent FileStateRegistry."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from oxenclaw.tools_pkg.file_state import FileStateRegistry, get_registry
from oxenclaw.tools_pkg.fs_tools import edit_tool, read_file_tool, write_file_tool


@pytest.fixture(autouse=True)
def _clean_registry():
    get_registry().clear()
    yield
    get_registry().clear()


def test_read_then_write_no_warning(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    reg = FileStateRegistry()
    reg.register_read("agent-A", p)
    assert reg.check_stale("agent-A", p) is None


def test_sibling_write_warns(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    reg = FileStateRegistry()
    reg.register_read("agent-A", p)
    # Sibling B writes after A's read.
    time.sleep(0.01)
    reg.register_write("agent-B", p)
    warn = reg.check_stale("agent-A", p)
    assert warn is not None
    assert warn.kind == "sibling_wrote"
    assert warn.last_writer == "agent-B"


def test_mtime_drift_warns(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    reg = FileStateRegistry()
    reg.register_read("agent-A", p)
    # External edit — bump mtime.
    time.sleep(0.01)
    p.write_text("hello world\n")
    os.utime(p, (time.time() + 5, time.time() + 5))
    warn = reg.check_stale("agent-A", p)
    assert warn is not None
    assert warn.kind == "mtime_drift"


def test_write_without_read_warns_when_file_exists(tmp_path: Path) -> None:
    p = tmp_path / "exists.txt"
    p.write_text("hi\n")
    reg = FileStateRegistry()
    warn = reg.check_stale("agent-A", p)
    assert warn is not None
    assert warn.kind == "write_without_read"


def test_write_to_new_path_no_warning(tmp_path: Path) -> None:
    reg = FileStateRegistry()
    new_path = tmp_path / "new.txt"
    assert reg.check_stale("agent-A", new_path) is None


def test_partial_read_warns_on_next_write(tmp_path: Path) -> None:
    p = tmp_path / "a.txt"
    p.write_text("hello\n")
    reg = FileStateRegistry()
    reg.register_read("agent-A", p, partial=True)
    warn = reg.check_stale("agent-A", p)
    assert warn is not None
    assert warn.kind == "mtime_drift"


def test_writes_since_returns_sibling_paths(tmp_path: Path) -> None:
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("x")
    p2.write_text("y")
    reg = FileStateRegistry()
    reg.register_read("agent-A", p1)
    reg.register_read("agent-A", p2)
    time.sleep(0.01)
    reg.register_write("agent-B", p2)
    out = reg.writes_since("agent-A", [p1, p2])
    assert str(p2.resolve()) in out
    assert str(p1.resolve()) not in out


async def test_fs_tools_read_registers(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("alpha\nbeta\n")
    tool = read_file_tool()
    await tool.execute({"path": str(p)})
    reg = get_registry()
    # check_stale should NOT warn — fresh read just happened.
    assert reg.check_stale("main", p) is None


async def test_fs_tools_write_warns_on_sibling(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("orig\n")
    reg = get_registry()
    # Simulate sibling agent wrote first.
    reg.register_write("sibling-X", p)
    # Now main agent tries to write without reading.
    tool = write_file_tool()
    out = await tool.execute({"path": str(p), "content": "new\n"})
    assert "WARN" in out
    assert p.read_text() == "new\n"


async def test_fs_tools_edit_warns_on_sibling(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    p.write_text("foo\n")
    reg = get_registry()
    reg.register_write("sibling-X", p)
    tool = edit_tool()
    out = await tool.execute(
        {"path": str(p), "old_str": "foo", "new_str": "bar"}
    )
    assert "WARN" in out
    assert "edited" in out
    assert p.read_text() == "bar\n"
