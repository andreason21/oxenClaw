"""Context-window guard pre-flight check."""

from __future__ import annotations

import pytest

from oxenclaw.pi.run.context_window_guard import (
    CONTEXT_WINDOW_HARD_MIN_TOKENS,
    CONTEXT_WINDOW_WARN_BELOW_TOKENS,
    ContextWindowTooSmallError,
    assert_context_window_usable,
    evaluate_context_window_guard,
)


def test_evaluate_warn_below_recommended_floor() -> None:
    v = evaluate_context_window_guard(20_000)
    assert v.tokens == 20_000
    assert v.should_warn is True
    assert v.should_block is False


def test_evaluate_block_below_hard_minimum() -> None:
    v = evaluate_context_window_guard(8_192)
    assert v.should_block is True
    assert v.should_warn is True


def test_evaluate_clean_at_or_above_warn_floor() -> None:
    v = evaluate_context_window_guard(CONTEXT_WINDOW_WARN_BELOW_TOKENS)
    assert v.should_warn is False
    assert v.should_block is False
    big = evaluate_context_window_guard(262_144)
    assert big.should_warn is False
    assert big.should_block is False


def test_evaluate_handles_unknown_window() -> None:
    """tokens=None / 0 / negative → both flags False (no spurious block)."""
    for value in (None, 0, -1):
        v = evaluate_context_window_guard(value)  # type: ignore[arg-type]
        assert v.should_warn is False
        assert v.should_block is False


def test_assert_raises_below_hard_min() -> None:
    with pytest.raises(ContextWindowTooSmallError):
        assert_context_window_usable("toy:1k", 1_024)


def test_assert_warns_below_recommended() -> None:
    # No raise; just exercises the warn path.
    v = assert_context_window_usable("smallish", 20_000)
    assert v.should_warn is True
    assert v.should_block is False


def test_thresholds_match_upstream() -> None:
    """Pin the constants so a careless edit doesn't drift from openclaw."""
    assert CONTEXT_WINDOW_HARD_MIN_TOKENS == 16_000
    assert CONTEXT_WINDOW_WARN_BELOW_TOKENS == 32_000
