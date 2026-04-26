"""MMR re-ranking unit tests + tokenizer behavior."""

from __future__ import annotations

from oxenclaw.memory.mmr import MMRConfig, jaccard_similarity, mmr_rerank, tokenize
from oxenclaw.memory.models import MemoryChunk, MemorySearchResult


def _r(id_: str, text: str, score: float) -> MemorySearchResult:
    chunk = MemoryChunk(
        id=id_,
        path=f"{id_}.md",
        source="memory",
        start_line=1,
        end_line=1,
        text=text,
        hash="h",
    )
    return MemorySearchResult(chunk=chunk, score=score, distance=1.0 - score)


def test_disabled_returns_original_order() -> None:
    items = [_r("a", "x", 0.5), _r("b", "y", 0.9)]
    out = mmr_rerank(items, config=MMRConfig(enabled=False))
    assert [r.chunk.id for r in out] == ["a", "b"]


def test_lambda_one_collapses_to_score_sort() -> None:
    items = [_r("a", "alpha", 0.3), _r("b", "beta", 0.9), _r("c", "gamma", 0.5)]
    out = mmr_rerank(items, config=MMRConfig(enabled=True, lambda_=1.0))
    assert [r.chunk.id for r in out] == ["b", "c", "a"]


def test_diversity_picks_dissimilar_second() -> None:
    """With λ low (diversity-heavy), the second pick should be the item that
    overlaps least in tokens with the first, even if its raw score is lower."""
    items = [
        _r("hi1", "alpha beta gamma", 0.95),
        _r("dup", "alpha beta gamma delta", 0.94),  # near-duplicate
        _r("div", "completely different content", 0.50),
    ]
    out = mmr_rerank(items, config=MMRConfig(enabled=True, lambda_=0.2))
    ids = [r.chunk.id for r in out]
    assert ids[0] == "hi1"
    assert ids[1] == "div"  # diverse, despite lower score
    assert ids[2] == "dup"


def test_handles_empty_and_single() -> None:
    assert mmr_rerank([], config=MMRConfig(enabled=True)) == []
    one = [_r("a", "x", 0.5)]
    assert mmr_rerank(one, config=MMRConfig(enabled=True)) == one


def test_jaccard_edge_cases() -> None:
    assert jaccard_similarity(set(), set()) == 1.0
    assert jaccard_similarity({"a"}, set()) == 0.0
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard_similarity({"a", "b"}, {"b", "c"}) == 1 / 3


def test_tokenize_cjk_bigrams_and_ascii() -> None:
    tokens = tokenize("Hello 我喜欢 world")
    assert "hello" in tokens
    assert "world" in tokens
    assert "我" in tokens and "喜" in tokens and "欢" in tokens
    # Adjacent CJK bigrams are emitted.
    assert "我喜" in tokens and "喜欢" in tokens


def test_large_pool_completes_quickly() -> None:
    """Smoke test the O(N²) path completes on a realistic pool size."""
    import time

    items = [_r(f"id{i}", f"alpha beta {i}", 1.0 - i / 200) for i in range(100)]
    t0 = time.monotonic()
    out = mmr_rerank(items, config=MMRConfig(enabled=True, lambda_=0.5))
    elapsed = time.monotonic() - t0
    assert len(out) == 100
    # Generous budget — incremental algo should finish in well under 1s.
    assert elapsed < 1.0
