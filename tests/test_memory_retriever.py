"""MemoryRetriever save/search round-trip."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.retriever import (
    MemoryRetriever,
    format_memories_as_prelude,
    format_memories_for_prompt,
)
from oxenclaw.memory.store import MemoryStore
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


async def test_for_root_wires_defaults(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    assert r.memory_dir == tmp_path / "memory"
    assert r.inbox_path == tmp_path / "memory" / "inbox.md"
    await r.aclose()


async def test_save_then_search_roundtrip(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        report = await r.save("the secret password is bluefish-42", tags=["fact"])
        assert report.added + report.changed >= 1
        hits = await r.search("password", k=5)
        assert hits
        assert any("bluefish" in h.chunk.text for h in hits)
    finally:
        await r.aclose()


async def test_empty_query_returns_empty(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        assert await r.search("") == []
        assert await r.search("   ") == []
    finally:
        await r.aclose()


async def test_format_memories_for_prompt_empty() -> None:
    assert format_memories_for_prompt([]) == ""


async def test_format_memories_for_prompt_renders_citations(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("alpha beta gamma")
        hits = await r.search("alpha")
        rendered = format_memories_for_prompt(hits)
        assert "<recalled_memories>" in rendered
        assert "citation=" in rendered
        assert "inbox.md" in rendered
    finally:
        await r.aclose()


async def test_format_memories_as_prelude_empty() -> None:
    assert format_memories_as_prelude([]) == ""


async def test_format_memories_as_prelude_is_plain_bullet_list(tmp_path: Path) -> None:
    """Prelude is the front-of-prompt redundancy meant for small local
    models — must be plain text (no XML, no escape sequences) and
    must contain the recalled chunk content as a bullet item."""
    r = _retriever(tmp_path)
    try:
        await r.save("User lives in Suwon, South Korea.")
        hits = await r.search("Suwon")
        rendered = format_memories_as_prelude(hits)
        assert rendered.startswith("## What you already know about this user")
        assert "<" not in rendered  # no XML
        assert "Suwon" in rendered
        # Positive framing only — no "Never reply..." style negative
        # imperatives. Small local models freeze and emit empty replies
        # when given negative meta-instructions; the directive must
        # state what TO do, not what to avoid.
        assert "Never" not in rendered
        assert "do NOT" not in rendered
        assert "Use them" in rendered
    finally:
        await r.aclose()


async def test_format_memories_as_prelude_truncates_long_chunks(tmp_path: Path) -> None:
    """Long chunks get truncated to 280 chars + ellipsis so the prelude
    stays small. The XML block carries the full text for citation-aware
    consumers."""
    r = _retriever(tmp_path)
    try:
        long_text = "fact " * 200  # ~1000 chars
        await r.save(long_text)
        hits = await r.search("fact")
        rendered = format_memories_as_prelude(hits)
        assert "…" in rendered
        # bullet line capped well under the raw chunk length
        bullet_line = next(line for line in rendered.splitlines() if line.startswith("- "))
        assert len(bullet_line) <= 290  # "- " + 280 + "…"
    finally:
        await r.aclose()


async def test_get_returns_inbox_slice(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("entry one", tags=["x"])
        result = r.get("inbox.md", from_line=1, lines=20)
        assert "entry one" in result.text
    finally:
        await r.aclose()


async def test_construct_with_explicit_components(tmp_path: Path) -> None:
    """Direct __init__ wiring without `for_root`."""
    store = MemoryStore(tmp_path / "i.sqlite")
    cache = EmbeddingCache(StubEmbeddings(), store)
    mem = tmp_path / "m"
    mem.mkdir()
    r = MemoryRetriever(store, cache, mem, mem / "inbox.md")
    assert r.store is store
    assert r.memory_dir == mem
    await r.aclose()
