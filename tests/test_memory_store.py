"""MemoryStore schema + chunk replace + search behaviours."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.memory.hashing import sha256_text
from oxenclaw.memory.store import MemoryStore


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "index.sqlite")


def _emb(values: list[float]) -> list[float]:
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def test_schema_init_idempotent(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.close()
    # Second open must succeed without errors.
    s2 = MemoryStore(tmp_path / "index.sqlite")
    assert s2.count_files() == 0
    s2.close()


def test_ensure_schema_meta_dim_mismatch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    with pytest.raises(ValueError, match="rebuild"):
        s.ensure_schema_meta("stub", "m1", 8)
    s.close()


def test_ensure_schema_meta_model_mismatch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    with pytest.raises(ValueError, match="rebuild"):
        s.ensure_schema_meta("stub", "m2", 4)
    s.close()


def test_replace_chunks_for_file_atomic(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    s.upsert_file("a.md", "memory", "h0", 1.0, 100)
    chunks_v1 = [
        (1, 5, "first text", sha256_text("first text"), _emb([1.0, 0.0, 0.0, 0.0])),
        (6, 10, "second text", sha256_text("second text"), _emb([0.0, 1.0, 0.0, 0.0])),
    ]
    s.replace_chunks_for_file("a.md", "memory", "m1", chunks_v1)
    assert s.count_chunks() == 2

    chunks_v2 = [
        (1, 3, "only one", sha256_text("only one"), _emb([0.0, 0.0, 1.0, 0.0])),
    ]
    s.replace_chunks_for_file("a.md", "memory", "m1", chunks_v2)
    assert s.count_chunks() == 1
    s.close()


def test_vector_search_returns_closest(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    s.upsert_file("a.md", "memory", "h0", 1.0, 100)
    chunks = [
        (1, 1, "alpha", sha256_text("alpha"), _emb([1.0, 0.0, 0.0, 0.0])),
        (2, 2, "beta", sha256_text("beta"), _emb([0.0, 1.0, 0.0, 0.0])),
        (3, 3, "gamma", sha256_text("gamma"), _emb([0.0, 0.0, 1.0, 0.0])),
    ]
    s.replace_chunks_for_file("a.md", "memory", "m1", chunks)
    hits = s.search_vector(_emb([0.95, 0.05, 0.0, 0.0]), k=2)
    assert hits
    assert hits[0][0].text == "alpha"
    s.close()


def test_fts_search_keyword_hit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    s.upsert_file("a.md", "memory", "h0", 1.0, 100)
    chunks = [
        (
            1,
            1,
            "the quick brown fox",
            sha256_text("a"),
            _emb([1.0, 0.0, 0.0, 0.0]),
        ),
        (
            2,
            2,
            "lazy dog naps",
            sha256_text("b"),
            _emb([0.0, 1.0, 0.0, 0.0]),
        ),
    ]
    s.replace_chunks_for_file("a.md", "memory", "m1", chunks)
    hits = s.search_fts("quick", k=5)
    assert any("quick" in c.text for c, _ in hits)
    s.close()


def test_clear_all_keeps_meta_and_cache(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    s.cache_put("stub", "m1", "h", _emb([1.0, 0.0, 0.0, 0.0]))
    s.upsert_file("a.md", "memory", "h0", 1.0, 100)
    s.replace_chunks_for_file(
        "a.md",
        "memory",
        "m1",
        [(1, 1, "x", sha256_text("x"), _emb([1.0, 0.0, 0.0, 0.0]))],
    )
    s.clear_all()
    assert s.count_chunks() == 0
    assert s.count_files() == 0
    assert s.cache_size() == 1
    assert s.read_meta().get("embedding_model") == "m1"
    s.close()


def test_delete_file_cascades_chunks_and_vec(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.ensure_schema_meta("stub", "m1", 4)
    s.upsert_file("a.md", "memory", "h0", 1.0, 100)
    s.replace_chunks_for_file(
        "a.md",
        "memory",
        "m1",
        [(1, 1, "z", sha256_text("z"), _emb([1.0, 0.0, 0.0, 0.0]))],
    )
    s.delete_file("a.md")
    assert s.count_chunks() == 0
    assert s.count_files() == 0
    s.close()
