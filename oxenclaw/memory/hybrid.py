"""Vector + BM25 hybrid search merge.

Mirrors openclaw `extensions/memory-core/src/memory/hybrid.ts`. Combines
semantic and keyword scores by ``vw * vector_score + tw * text_score`` and
returns the union sorted desc.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from oxenclaw.memory.models import MemorySearchResult

# Unicode word characters: any letter or number or underscore.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class HybridConfig:
    """Vector + keyword merge knobs."""

    enabled: bool = False
    vector_weight: float = 0.7
    text_weight: float = 0.3
    candidate_multiplier: int = 3


DEFAULT_HYBRID_CONFIG = HybridConfig()


def build_fts_query(raw: str) -> str | None:
    """Lowercase + unicode-word tokens, quoted and AND-joined.

    Returns ``None`` for empty or punctuation-only input so the caller can
    skip the FTS leg.
    """
    lower = raw.lower()
    tokens = [t for t in _WORD_RE.findall(lower) if t]
    if not tokens:
        return None
    quoted = [f'"{t.replace(chr(34), "")}"' for t in tokens]
    return " AND ".join(quoted)


def bm25_rank_to_score(rank: float) -> float:
    """Map fts5 bm25 rank (lower=better, usually negative) to ``[0, 1]``."""
    if not math.isfinite(rank):
        return 1.0 / (1.0 + 999.0)
    if rank < 0:
        relevance = -rank
        return relevance / (1.0 + relevance)
    return 1.0 / (1.0 + rank)


def merge_hybrid_results(
    *,
    vector: list[MemorySearchResult],
    keyword: list[MemorySearchResult],
    config: HybridConfig = DEFAULT_HYBRID_CONFIG,
) -> list[MemorySearchResult]:
    """Merge vector + keyword hits by chunk id, weight, sort desc."""
    by_id: dict[str, tuple[MemorySearchResult, float, float]] = {}
    # tuple = (representative result, vector_score, text_score)

    for r in vector:
        by_id[r.chunk.id] = (r, r.score, 0.0)

    for r in keyword:
        if r.chunk.id in by_id:
            existing, vs, _ts = by_id[r.chunk.id]
            by_id[r.chunk.id] = (existing, vs, r.score)
        else:
            by_id[r.chunk.id] = (r, 0.0, r.score)

    vw = config.vector_weight
    tw = config.text_weight
    merged: list[MemorySearchResult] = []
    for rep, vs, ts in by_id.values():
        combined = vw * vs + tw * ts
        merged.append(
            MemorySearchResult(
                chunk=rep.chunk,
                score=combined,
                distance=rep.distance,
            )
        )
    merged.sort(key=lambda r: r.score, reverse=True)
    return merged
