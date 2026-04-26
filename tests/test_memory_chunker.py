"""Markdown chunker boundary cases."""

from __future__ import annotations

from sampyclaw.memory.chunker import chunk_markdown


def test_empty_input_returns_empty_list() -> None:
    assert chunk_markdown("") == []


def test_single_paragraph_yields_one_chunk_covering_all_lines() -> None:
    text = "alpha\nbeta\ngamma"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    assert len(result) == 1
    start, end, body = result[0]
    assert start == 1
    assert end == 3
    assert "alpha" in body and "gamma" in body


def test_same_level_headings_split_into_distinct_chunks() -> None:
    """Same-level (or higher) sibling headings open new sections."""
    text = "# Top A\nbody A\n# Top B\nbody B\n"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    bodies = [b for _, _, b in result]
    a = next(b for b in bodies if "Top A" in b)
    assert "Top B" not in a
    b = next(b for b in bodies if "Top B" in b)
    assert "Top A" not in b


def test_lower_level_headings_stay_within_parent() -> None:
    """`##` under `#` is grouped with the parent until the next `#` (or eof)."""
    text = "# Parent\nintro\n## Child\nchild body\n"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    assert len(result) == 1
    _, _, body = result[0]
    assert "Parent" in body and "Child" in body


def test_oversize_paragraph_hard_split() -> None:
    text = "x" * 5000
    result = chunk_markdown(text, max_chars=1000, min_chars=0)
    assert len(result) >= 5
    for _, _, body in result:
        assert len(body) <= 1000


def test_line_ranges_are_one_indexed_inclusive() -> None:
    text = "# H1\nA\n# H2\nB\nC\n"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    # Two heading sections (the trailing blank line attaches to last).
    starts = [s for s, _, _ in result]
    ends = [e for _, e, _ in result]
    assert starts[0] == 1
    assert ends[0] == 2  # H1 + A
    assert starts[1] == 3  # H2 line


def test_preamble_without_heading_becomes_first_chunk() -> None:
    text = "preamble\n\n# Heading\nbody\n"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    assert any(b.startswith("preamble") for _, _, b in result)


def test_trailing_whitespace_is_stripped() -> None:
    text = "alpha   \nbeta\n"
    result = chunk_markdown(text, max_chars=2000, min_chars=0)
    assert all(not b.endswith(" ") and not b.endswith("\n") for _, _, b in result)


def test_small_chunks_merged_to_min_chars() -> None:
    text = "# A\n" + "## H\n" * 6
    merged = chunk_markdown(text, max_chars=2000, min_chars=200)
    unmerged = chunk_markdown(text, max_chars=2000, min_chars=0)
    assert len(merged) <= len(unmerged)
