"""Memory inbox dedup — same content updates instead of duplicating.

The motivating real-world inbox snapshot had 11 essentially-identical
"user lives in Suwon" rows because every regex backstop / turn_dream
/ model `memory_save` call wrote a fresh section. After this fix:

  - Layer 1 (normalised exact match) folds case / whitespace /
    trailing-punctuation variants onto one entry.
  - Layer 2 (vector similarity ≥ threshold) folds paraphrases when
    real embeddings are configured.
  - Dedup replaces the existing section in place: old removed, new
    appended with current timestamp + merged tags. Net effect: one
    entry per fact, recency tracks the latest mention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.inbox import (
    normalize_for_dedup,
    parse_inbox,
    remove_inbox_entry,
)
from oxenclaw.memory.retriever import MemoryRetriever
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


# ─── normalize_for_dedup ────────────────────────────────────────────


def test_normalize_collapses_case_whitespace_punct() -> None:
    base = "user lives in suwon"
    for variant in (
        "User lives in Suwon",
        "user lives in suwon.",
        "  USER LIVES IN SUWON!  ",
        "user lives in   suwon",
        "user lives in suwon。",  # CJK full stop
        "user\tlives\nin suwon",
    ):
        assert normalize_for_dedup(variant) == base, variant


def test_normalize_distinguishes_different_content() -> None:
    assert normalize_for_dedup("user lives in Suwon") != normalize_for_dedup(
        "user lives in Seoul"
    )
    assert normalize_for_dedup("user is named Bob") != normalize_for_dedup(
        "user lives in Bob"
    )


# ─── parse_inbox / remove_inbox_entry ───────────────────────────────


def test_parse_inbox_round_trip(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.md"
    inbox.write_text(
        "\n## 2026-04-29T10:00:00+09:00\n\n"
        "**tags:** auto, location\n\n"
        "user lives in Suwon\n\n"
        "## 2026-04-29T11:00:00+09:00\n\n"
        "user's brother is 민수\n",
        encoding="utf-8",
    )
    entries = parse_inbox(inbox)
    assert len(entries) == 2
    assert entries[0].when == "2026-04-29T10:00:00+09:00"
    assert entries[0].body == "user lives in Suwon"
    assert entries[0].tags == ["auto", "location"]
    assert entries[1].when == "2026-04-29T11:00:00+09:00"
    assert entries[1].body == "user's brother is 민수"
    assert entries[1].tags == []


def test_remove_inbox_entry_drops_section(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox.md"
    inbox.write_text(
        "\n## A\n\nbody A\n\n## B\n\nbody B\n\n## C\n\nbody C\n",
        encoding="utf-8",
    )
    entries = parse_inbox(inbox)
    assert [e.body for e in entries] == ["body A", "body B", "body C"]
    removed = remove_inbox_entry(inbox, entries[1])
    assert removed is True
    after = parse_inbox(inbox)
    assert [e.body for e in after] == ["body A", "body C"]


def test_parse_inbox_handles_missing_file(tmp_path: Path) -> None:
    assert parse_inbox(tmp_path / "nope.md") == []


# ─── MemoryRetriever.save dedup behaviour ──────────────────────────


async def test_save_appends_new_entry(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        report = await r.save("user lives in Suwon", tags=["auto"])
        assert report.dedup_replaced is False
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 1
        assert entries[0].body == "user lives in Suwon"
        assert entries[0].tags == ["auto"]
    finally:
        await r.aclose()


async def test_save_dedupes_exact_repeat(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("user lives in Suwon", tags=["auto"])
        report = await r.save("user lives in Suwon", tags=["personal-fact"])
        assert report.dedup_replaced is True
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 1, (
            f"expected 1 entry after dedup; got {len(entries)}: "
            f"{[e.body for e in entries]}"
        )
        # Tags merged across the two saves.
        assert set(entries[0].tags) == {"auto", "personal-fact"}
    finally:
        await r.aclose()


async def test_save_dedupes_case_and_punct_variants(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("user lives in Suwon", tags=["v1"])
        await r.save("User lives in Suwon.", tags=["v2"])
        await r.save("  USER LIVES IN SUWON!  ", tags=["v3"])
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 1, (
            f"all variants should fold into one entry; got "
            f"{[e.body for e in entries]}"
        )
        assert set(entries[0].tags) == {"v1", "v2", "v3"}
    finally:
        await r.aclose()


async def test_save_keeps_distinct_facts_separate(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("user lives in Suwon", tags=["loc"])
        await r.save("user's brother is Minsu", tags=["family"])
        await r.save("user prefers dark mode", tags=["pref"])
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 3
        assert {e.body for e in entries} == {
            "user lives in Suwon",
            "user's brother is Minsu",
            "user prefers dark mode",
        }
    finally:
        await r.aclose()


async def test_save_dedup_refreshes_timestamp(tmp_path: Path) -> None:
    """The 'update' contract: re-saving the same fact moves its
    timestamp to now, so the temporal-decay layer treats it as
    fresh."""
    import asyncio

    r = _retriever(tmp_path)
    try:
        await r.save("user lives in Suwon")
        first_when = parse_inbox(r.inbox_path)[0].when
        await asyncio.sleep(0.01)  # ensure timestamps differ
        await r.save("user lives in Suwon")
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 1
        assert entries[0].when != first_when, (
            "dedup should have rewritten the timestamp to the new save"
        )
    finally:
        await r.aclose()


async def test_save_dedup_can_be_disabled(tmp_path: Path) -> None:
    """`dedup=False` lets callers (e.g. bulk imports, tests) write
    repeated content without folding."""
    r = _retriever(tmp_path)
    try:
        await r.save("repeated fact", dedup=False)
        await r.save("repeated fact", dedup=False)
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 2
    finally:
        await r.aclose()


@pytest.mark.parametrize(
    "first,second",
    [
        ("user lives in Suwon", "user lives in Seoul"),
        ("user has a brother", "user has a sister"),
    ],
)
async def test_save_does_not_dedup_distinct_short_facts(
    tmp_path: Path, first: str, second: str
) -> None:
    """Layer 1 normalisation only folds true duplicates. Distinct
    short facts that differ by one token must not be collapsed."""
    r = _retriever(tmp_path)
    try:
        await r.save(first)
        await r.save(second)
        entries = parse_inbox(r.inbox_path)
        assert len(entries) == 2, [e.body for e in entries]
    finally:
        await r.aclose()


async def test_save_tool_message_changes_on_dedup(tmp_path: Path) -> None:
    """The `memory_save` tool surfaces dedup with a friendly
    message instead of `0 chunks` confusion."""
    from oxenclaw.memory.tools import memory_save_tool

    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        first = await tool.execute({"text": "user lives in Suwon"})
        assert "saved to" in first
        second = await tool.execute({"text": "user lives in Suwon"})
        assert "updated existing entry" in second
    finally:
        await r.aclose()
