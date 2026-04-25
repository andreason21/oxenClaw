"""read_file_range happy path + traversal protection."""

from __future__ import annotations

from pathlib import Path

import pytest

from sampyclaw.memory.reader import read_file_range


def test_happy_slice(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("\n".join(f"line {i}" for i in range(1, 11)))
    res = read_file_range(tmp_path, "a.md", from_line=2, lines=3)
    assert res.start_line == 2
    assert res.end_line == 4
    assert "line 2" in res.text
    assert "line 4" in res.text
    assert "line 5" not in res.text


def test_truncation_sets_next_from(tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("\n".join(f"line {i}" for i in range(1, 11)))
    res = read_file_range(tmp_path, "a.md", from_line=1, lines=3)
    assert res.truncated is True
    assert res.next_from == 4


def test_traversal_attempt_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_file_range(tmp_path, "../etc/passwd")


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    res = read_file_range(tmp_path, "missing.md")
    assert res.text == ""
    assert res.truncated is False


def test_max_chars_hard_truncates_single_long_line(tmp_path: Path) -> None:
    p = tmp_path / "big.md"
    p.write_text("x" * 5000)
    res = read_file_range(tmp_path, "big.md", from_line=1, lines=1, max_chars=100)
    assert len(res.text) <= 100
    assert res.truncated is True
