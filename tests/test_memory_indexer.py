"""MemoryIndexer incremental sync."""

from __future__ import annotations

import time
from pathlib import Path

from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.indexer import MemoryIndexer
from oxenclaw.memory.store import MemoryStore
from tests._memory_stubs import StubEmbeddings


def _wire(tmp_path: Path) -> tuple[MemoryStore, EmbeddingCache, Path, MemoryIndexer]:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    store = MemoryStore(tmp_path / "index.sqlite")
    cache = EmbeddingCache(StubEmbeddings(), store)
    indexer = MemoryIndexer(store, cache, mem_dir)
    return store, cache, mem_dir, indexer


async def test_first_sync_indexes_all_files(tmp_path: Path) -> None:
    store, _cache, mem_dir, indexer = _wire(tmp_path)
    (mem_dir / "a.md").write_text("# A\nbody A\n")
    (mem_dir / "b.md").write_text("# B\nbody B\n")
    report = await indexer.sync()
    assert report.added == 2
    assert report.changed == 0
    assert store.count_files() == 2
    assert store.count_chunks() >= 2
    store.close()


async def test_second_sync_zero_when_unchanged(tmp_path: Path) -> None:
    store, _cache, mem_dir, indexer = _wire(tmp_path)
    (mem_dir / "a.md").write_text("# A\nbody\n")
    await indexer.sync()
    report = await indexer.sync()
    assert report.added == 0
    assert report.changed == 0
    assert report.deleted == 0
    store.close()


async def test_modifying_file_reindexes_only_that_file(tmp_path: Path) -> None:
    store, _cache, mem_dir, indexer = _wire(tmp_path)
    (mem_dir / "a.md").write_text("# A\nbody A\n")
    (mem_dir / "b.md").write_text("# B\nbody B\n")
    await indexer.sync()
    time.sleep(0.01)
    (mem_dir / "a.md").write_text("# A\nbody A updated\n")
    report = await indexer.sync()
    assert report.changed == 1
    assert report.added == 0
    assert report.deleted == 0
    store.close()


async def test_deleted_file_removed(tmp_path: Path) -> None:
    store, _cache, mem_dir, indexer = _wire(tmp_path)
    (mem_dir / "a.md").write_text("# A\nbody\n")
    (mem_dir / "b.md").write_text("# B\nbody\n")
    await indexer.sync()
    (mem_dir / "b.md").unlink()
    report = await indexer.sync()
    assert report.deleted == 1
    assert store.count_files() == 1
    store.close()
