"""Tests for the 3-layer tool-result persistence module."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.pi.tool_result_storage import (
    BudgetConfig,
    enforce_turn_budget,
    maybe_persist_tool_result,
    resolve_threshold,
)


def test_small_output_passes_through(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=100)
    out = maybe_persist_tool_result(
        tool_use_id="t1",
        tool_name="grep",
        output="hello world",
        config=cfg,
        storage_dir=tmp_path,
    )
    assert out == "hello world"
    assert list(tmp_path.iterdir()) == []  # no file written


def test_large_output_is_persisted(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=50)
    big = "x" * 500
    out = maybe_persist_tool_result(
        tool_use_id="t2",
        tool_name="grep",
        output=big,
        config=cfg,
        storage_dir=tmp_path,
    )
    assert out != big
    assert "[Output persisted to" in out
    assert "Use read_file with offset/limit" in out
    target = tmp_path / "t2.txt"
    assert target.exists()
    assert target.read_text() == big


def test_atomic_write_no_tmp_leftover(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=10)
    big = "y" * 100
    maybe_persist_tool_result(
        tool_use_id="atomic",
        tool_name="grep",
        output=big,
        config=cfg,
        storage_dir=tmp_path,
    )
    # Only the final file should exist; no .tmp- leftover.
    files = list(tmp_path.iterdir())
    assert [f.name for f in files] == ["atomic.txt"]


def test_read_file_is_pinned_never_persisted(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=10)
    big = "z" * 100_000
    out = maybe_persist_tool_result(
        tool_use_id="r1",
        tool_name="read_file",
        output=big,
        config=cfg,
        storage_dir=tmp_path,
    )
    assert out == big
    assert list(tmp_path.iterdir()) == []


def test_resolve_threshold_pinned_vs_default() -> None:
    cfg = BudgetConfig(default_threshold=42)
    assert resolve_threshold("grep", cfg) == 42
    assert resolve_threshold("read_file", cfg) > 10**9
    assert resolve_threshold("memory_search", cfg) > 10**9


def test_unsafe_tool_use_id_rejected(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=5)
    with pytest.raises(ValueError):
        maybe_persist_tool_result(
            tool_use_id="../escape",
            tool_name="grep",
            output="abcdefghij",
            config=cfg,
            storage_dir=tmp_path,
        )


def test_turn_budget_persists_largest(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=10**9)  # disable per-result spilling
    results = [
        {"id": "a", "name": "grep", "output": "a" * 50_000},
        {"id": "b", "name": "grep", "output": "b" * 150_000},
        {"id": "c", "name": "grep", "output": "c" * 30_000},
    ]
    persisted_chars = enforce_turn_budget(
        results, cfg, tmp_path, turn_budget=100_000
    )
    # Largest entry (b) should have been spilled.
    assert persisted_chars >= 150_000
    assert (tmp_path / "b.txt").exists()
    # Others stay inline.
    assert results[0]["output"] == "a" * 50_000
    assert results[2]["output"] == "c" * 30_000
    # b is now a preview block.
    assert "[Output persisted to" in results[1]["output"]


def test_turn_budget_skips_pinned_tools(tmp_path: Path) -> None:
    cfg = BudgetConfig(default_threshold=10**9)
    results = [
        {"id": "r1", "name": "read_file", "output": "x" * 300_000},
        {"id": "g1", "name": "grep", "output": "y" * 50_000},
    ]
    enforce_turn_budget(results, cfg, tmp_path, turn_budget=100_000)
    # read_file is pinned — must NOT be persisted, even though it's the
    # largest. We accept that the budget might not be reachable.
    assert results[0]["output"] == "x" * 300_000
    assert not (tmp_path / "r1.txt").exists()


def test_turn_budget_under_budget_no_op(tmp_path: Path) -> None:
    cfg = BudgetConfig()
    results = [
        {"id": "x", "name": "grep", "output": "small"},
        {"id": "y", "name": "grep", "output": "tiny"},
    ]
    persisted_chars = enforce_turn_budget(
        results, cfg, tmp_path, turn_budget=100_000
    )
    assert persisted_chars == 0
    assert results[0]["output"] == "small"
    assert list(tmp_path.iterdir()) == []
