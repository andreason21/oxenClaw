"""Tests for the multi-strategy fuzzy find-and-replace patcher."""

from __future__ import annotations

import pytest

from oxenclaw.tools_pkg.fuzzy_match import (
    FuzzyMatchError,
    detect_escape_drift,
    fuzzy_find_and_replace,
)


def test_strategy_exact() -> None:
    new, strat = fuzzy_find_and_replace("hello world", "hello", "HELLO")
    assert new == "HELLO world"
    assert strat == "exact"


def test_strategy_line_trimmed() -> None:
    content = "    foo bar    \n  baz  \n"
    new, strat = fuzzy_find_and_replace(content, "foo bar\nbaz", "X\nY")
    assert strat == "line_trimmed"
    assert "X" in new and "Y" in new


def test_strategy_whitespace_normalized() -> None:
    content = "def    foo(  a,   b ):\n    pass\n"
    new, strat = fuzzy_find_and_replace(
        content,
        "def foo( a, b ):",
        "def bar(a, b):",
    )
    assert strat == "whitespace_normalized"
    assert "def bar(a, b):" in new


def test_strategy_indentation_flexible() -> None:
    # Content uses 8-space indent; pattern uses 4-space + trailing spaces
    # so line_trimmed cannot match (it strips both ends, but here the
    # pattern lines have INTERNAL leading whitespace differences only).
    # Use a hard case: pattern has *more* indent than content.
    content = "if a:\n    return x\n"
    new, strat = fuzzy_find_and_replace(
        content,
        "        if a:\n            return x",  # over-indented pattern
        "if a:\n    return y",
    )
    # line_trimmed will also catch this (it trims both ends), so accept either.
    assert strat in ("line_trimmed", "indentation_flexible")
    assert "return y" in new


def test_strategy_escape_normalized() -> None:
    content = "alpha\nbeta\n"
    new, strat = fuzzy_find_and_replace(content, "alpha\\nbeta", "X\nY")
    assert strat == "escape_normalized"
    assert "X\nY" in new


def test_strategy_unicode_normalized() -> None:
    # Smart quotes in content, plain quotes in pattern.
    content = "msg = “hello”\n"
    new, strat = fuzzy_find_and_replace(content, 'msg = "hello"', 'msg = "world"')
    assert strat == "unicode_normalized"
    assert 'msg = "world"' in new


def test_strategy_block_anchor() -> None:
    content = "def foo():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n    # done\n"
    # First 2 + last 2 lines exactly match; middle slightly different.
    pattern = (
        "def foo():\n"
        "    a = 1\n"
        "    b = 22\n"  # drift
        "    c = 33\n"  # drift
        "    return a + b + c\n"
        "    # done\n"
    )
    new, strat = fuzzy_find_and_replace(content, pattern, "REPLACED")
    assert strat in ("block_anchor", "context_aware")
    assert "REPLACED" in new


def test_strategy_context_aware_close_match() -> None:
    content = "alpha beta gamma delta epsilon zeta eta\n"
    # 0.92+ similarity — same length, one-char drift.
    pattern = "alpha beta gamma delta epsilon zeta etx\n"
    new, strat = fuzzy_find_and_replace(content, pattern, "X\n")
    assert strat == "context_aware"
    assert "X" in new


def test_count_mismatch_raises() -> None:
    with pytest.raises(FuzzyMatchError) as exc:
        fuzzy_find_and_replace("foo\nfoo\nfoo\n", "foo", "bar", expected_count=1)
    assert "expected 1" in str(exc.value)
    assert "found 3" in str(exc.value)


def test_ambiguous_match_with_correct_count() -> None:
    new, strat = fuzzy_find_and_replace("foo\nfoo\nfoo\n", "foo", "bar", expected_count=3)
    assert strat == "exact"
    assert new == "bar\nbar\nbar\n"


def test_no_match_raises() -> None:
    with pytest.raises(FuzzyMatchError) as exc:
        fuzzy_find_and_replace("hello world", "absent", "x")
    assert "no strategy" in str(exc.value).lower()


def test_detect_escape_drift_positive() -> None:
    content = "name = 'alice'"
    old = "name = \\'alice\\'"
    assert detect_escape_drift(content, old) is True


def test_detect_escape_drift_negative_when_present() -> None:
    # If the file legitimately contains \', it's not drift.
    content = "echo 'it\\'s fine'"
    old = "it\\'s"
    assert detect_escape_drift(content, old) is False


def test_escape_drift_blocks_fuzzy() -> None:
    content = "name = 'alice'\n"
    with pytest.raises(FuzzyMatchError) as exc:
        fuzzy_find_and_replace(content, "name = \\'alice\\'", "name = 'bob'")
    assert "escape-drift" in str(exc.value).lower()


def test_empty_old_str_raises() -> None:
    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace("foo", "", "bar")


def test_identical_old_new_raises() -> None:
    with pytest.raises(FuzzyMatchError):
        fuzzy_find_and_replace("foo", "foo", "foo")
