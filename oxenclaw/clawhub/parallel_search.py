"""Parallel skill-source search with timeout + dedup.

Fans out across every configured `SkillSource`, collects results inside
a 4 s per-source timeout budget, dedupes by `(slug, source_id)` and
returns up to `limit` refs ranked by trust level.

When a fresh `IndexSource` is present we skip remote sources — the
index already aggregates them, so additional fan-out wastes time.
"""

from __future__ import annotations

import concurrent.futures
import logging
from typing import Iterable

from oxenclaw.clawhub.sources.base import SkillRef, SkillSource

logger = logging.getLogger(__name__)

_PER_SOURCE_TIMEOUT_S = 4.0

# Ranked: lower number = higher trust.
_TRUST_RANK = {"official": 0, "mirror": 1, "community": 2}


def _rank(ref: SkillRef) -> int:
    return _TRUST_RANK.get((ref.trust_level or "community").lower(), 99)


def parallel_search_sources(
    sources: Iterable[SkillSource],
    query: str,
    limit: int = 10,
) -> list[SkillRef]:
    """Run `search(query, limit)` on every source in parallel.

    Each source gets a 4 s deadline. Errors / timeouts are silently
    dropped so one slow upstream doesn't block the rest.
    """
    src_list = list(sources)
    if not src_list:
        return []

    # If a fresh `IndexSource` is configured, prefer the aggregate.
    fresh_index = next(
        (s for s in src_list if getattr(s, "source_id", "") == "index" and getattr(s, "fresh", False)),
        None,
    )
    if fresh_index is not None:
        active_sources: list[SkillSource] = [fresh_index]
    else:
        active_sources = src_list

    seen: set[tuple[str, str]] = set()
    collected: list[SkillRef] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(active_sources))) as pool:
        futures = {
            pool.submit(_safe_search, s, query, limit): s for s in active_sources
        }
        for fut in concurrent.futures.as_completed(futures, timeout=_PER_SOURCE_TIMEOUT_S * len(active_sources)):
            try:
                refs = fut.result(timeout=_PER_SOURCE_TIMEOUT_S)
            except (concurrent.futures.TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.debug("source %s timed out / errored: %s", futures[fut].source_id, exc)
                continue
            for r in refs:
                key = (r.slug, r.source_id)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(r)

    collected.sort(key=_rank)
    return collected[:limit]


def _safe_search(source: SkillSource, query: str, limit: int) -> list[SkillRef]:
    try:
        return source.search(query, limit)
    except Exception as exc:  # noqa: BLE001
        logger.debug("source %s search raised: %s", source.source_id, exc)
        return []


__all__ = ["parallel_search_sources"]
