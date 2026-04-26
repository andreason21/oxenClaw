"""EmbeddingCache hit/miss + dim recording."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.store import MemoryStore
from tests._memory_stubs import StubEmbeddings


async def test_cache_miss_then_hit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "index.sqlite")
    stub = StubEmbeddings()
    cache = EmbeddingCache(stub, store)

    first = await cache.embed(["alpha", "beta"])
    assert stub.call_count == 1
    assert stub.total_texts == 2
    assert cache.cache_hits == 0
    assert len(first) == 2

    second = await cache.embed(["alpha", "beta"])
    assert stub.call_count == 1  # provider not called again
    assert cache.cache_hits == 2
    # Cached values round-trip through float32 packing; compare with tolerance.
    for a_row, b_row in zip(first, second, strict=True):
        for a, b in zip(a_row, b_row, strict=True):
            assert abs(a - b) < 1e-5
    store.close()


async def test_partial_hit_only_misses_called(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "index.sqlite")
    stub = StubEmbeddings()
    cache = EmbeddingCache(stub, store)
    await cache.embed(["alpha"])
    assert stub.total_texts == 1
    await cache.embed(["alpha", "gamma"])
    # alpha cached, gamma fresh => one new text fetched
    assert stub.total_texts == 2
    assert cache.cache_hits == 1
    store.close()


async def test_dims_recorded(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "index.sqlite")
    stub = StubEmbeddings(dims=8)
    cache = EmbeddingCache(stub, store)
    await cache.embed(["x"])
    row = store.cache_get("stub", "stub-model", _content_hash("x"))
    assert row is not None
    assert len(row) == 8
    store.close()


def _content_hash(t: str) -> str:
    from oxenclaw.memory.hashing import sha256_text

    return sha256_text(t)


async def test_empty_input_returns_empty(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "index.sqlite")
    cache = EmbeddingCache(StubEmbeddings(), store)
    out = await cache.embed([])
    assert out == []
    assert cache.cache_hits == 0
    store.close()
