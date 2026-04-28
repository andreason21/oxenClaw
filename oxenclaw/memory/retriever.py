"""High-level memory facade — owns store, indexer, embeddings.

Search returns chunk-shaped `MemorySearchResult` items so the agent can
cite `path:start-end`. `save()` appends to a single inbox file then runs
an incremental re-index. The pipeline supports optional hybrid (vector +
BM25), MMR diversity re-ranking, and temporal half-life decay layers.
"""

from __future__ import annotations

from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.embeddings import EmbeddingProvider
from oxenclaw.memory.hybrid import (
    HybridConfig,
    build_fts_query,
    merge_hybrid_results,
)
from oxenclaw.memory.inbox import append_to_inbox
from oxenclaw.memory.indexer import MemoryIndexer
from oxenclaw.memory.mmr import MMRConfig, mmr_rerank
from oxenclaw.memory.models import (
    MemoryReadResult,
    MemorySearchResult,
    SyncReport,
)
from oxenclaw.memory.reader import read_file_range
from oxenclaw.memory.store import MemoryStore
from oxenclaw.memory.temporal_decay import (
    TemporalDecayConfig,
    apply_temporal_decay_to_results,
)
from oxenclaw.memory.walker import WalkerConfig
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.retriever")

DEFAULT_INBOX_FILE = "inbox.md"
DEFAULT_DB_FILE = "index.sqlite"


class MemoryRetriever:
    """Convenience facade over the memory subsystem."""

    def __init__(
        self,
        store: MemoryStore,
        embeddings_cache: EmbeddingCache,
        memory_dir: Path,
        inbox_path: Path,
        *,
        redact_level: str | None = None,
        walker_config: WalkerConfig | None = None,
    ) -> None:
        self._store = store
        self._embeddings = embeddings_cache
        self._memory_dir = memory_dir
        self._inbox_path = inbox_path
        self._redact_level = redact_level
        self._walker_config = walker_config
        self._indexer = MemoryIndexer(store, embeddings_cache, memory_dir)

    @classmethod
    def for_root(
        cls,
        paths: OxenclawPaths,
        embeddings: EmbeddingProvider,
        *,
        redact_level: str | None = None,
        walker_config: WalkerConfig | None = None,
    ) -> MemoryRetriever:
        memory_dir = paths.home / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        db_path = memory_dir / DEFAULT_DB_FILE
        store = MemoryStore(db_path)
        cache = EmbeddingCache(embeddings, store)
        inbox_path = memory_dir / DEFAULT_INBOX_FILE
        return cls(
            store,
            cache,
            memory_dir,
            inbox_path,
            redact_level=redact_level,
            walker_config=walker_config,
        )

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def memory_dir(self) -> Path:
        return self._memory_dir

    @property
    def inbox_path(self) -> Path:
        return self._inbox_path

    async def sync(self) -> SyncReport:
        return await self._indexer.sync()

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        source: str | None = None,
        hybrid: HybridConfig | None = None,
        mmr: MMRConfig | None = None,
        temporal_decay: TemporalDecayConfig | None = None,
        now_seconds: float | None = None,
    ) -> list[MemorySearchResult]:
        if not query.strip():
            return []
        vectors = await self._embeddings.embed([query])
        if not vectors:
            return []
        query_vec = vectors[0]

        hybrid_on = hybrid is not None and hybrid.enabled
        mmr_on = mmr is not None and mmr.enabled
        decay_on = temporal_decay is not None and temporal_decay.enabled

        # Decide oversample factor. Hybrid uses its own multiplier; MMR
        # alone wants a generous pool for diversity to matter.
        if hybrid_on:
            assert hybrid is not None  # narrow for pyright
            pool_k = max(k * hybrid.candidate_multiplier, k)
        elif mmr_on:
            pool_k = max(k * 3, k + 5)
        else:
            pool_k = k

        # 1. Candidate retrieval.
        if hybrid_on:
            assert hybrid is not None
            vec_hits = self._store.search_vector(query_vec, k=pool_k, source=source)
            vector_results = [
                MemorySearchResult(chunk=ch, score=max(0.0, 1.0 - dist), distance=dist)
                for ch, dist in vec_hits
            ]
            fts_query = build_fts_query(query)
            keyword_results: list[MemorySearchResult] = []
            if fts_query is not None:
                fts_hits = self._store.search_fts(fts_query, k=pool_k, source=source)
                keyword_results = [
                    MemorySearchResult(chunk=ch, score=score, distance=0.0)
                    for ch, score in fts_hits
                ]
            results = merge_hybrid_results(
                vector=vector_results,
                keyword=keyword_results,
                config=hybrid,
            )
        else:
            vec_hits = self._store.search_vector(query_vec, k=pool_k, source=source)
            results = [
                MemorySearchResult(chunk=ch, score=max(0.0, 1.0 - dist), distance=dist)
                for ch, dist in vec_hits
            ]

        # 2. Temporal decay (modulates relevance before MMR re-ranks).
        if decay_on:
            assert temporal_decay is not None
            mtimes = self._store.chunk_file_mtimes()
            results = apply_temporal_decay_to_results(
                results,
                file_mtimes=mtimes,
                config=temporal_decay,
                now_seconds=now_seconds,
            )

        # 3. MMR diversity re-ranking.
        if mmr_on:
            assert mmr is not None
            results = mmr_rerank(results, config=mmr)

        return results[:k]

    async def save(
        self,
        text: str,
        *,
        tags: list[str] | None = None,
        redact_level: str | None = None,
    ) -> SyncReport:
        effective_level = redact_level if redact_level is not None else self._redact_level
        append_to_inbox(self._inbox_path, text, tags=tags, redact_level=effective_level)
        return await self._indexer.sync()

    def get(
        self,
        rel_path: str,
        *,
        from_line: int = 1,
        lines: int = 120,
    ) -> MemoryReadResult:
        return read_file_range(self._memory_dir, rel_path, from_line=from_line, lines=lines)

    async def aclose(self) -> None:
        await self._embeddings.aclose()


def format_memories_for_prompt(results: list[MemorySearchResult]) -> str:
    """Render retrieved chunks as XML suitable for a system prompt.

    Each memory carries an `id` (chunk_id) + a `citation` string of the
    form `path:start-end`. We instruct the model to cite via
    `[mem:<id>]` when answering from a memory — short token, hashable,
    machine-parseable for any downstream UI that wants to highlight
    which span backs which assertion. Mirrors openclaw's
    `<memory id=…>` + `[mem:…]` convention.
    """
    if not results:
        return ""
    lines = [
        "<recalled_memories>",
        "  <usage>The chunks below ARE things you already know about this "
        "user — they were saved by you (or the operator) in earlier turns "
        "and retrieved just now because they're relevant to the current "
        "question. Treat them as authoritative ground truth about the "
        "user / project / past decisions. If the user asks something "
        'these chunks answer (e.g. "내가 어디 살지?" + a memory saying '
        '"User lives in Suwon"), USE the memory — do NOT respond with '
        '"I don\'t know" or "I have no record of that." When you answer '
        "from a specific memory, cite it inline with `[mem:&lt;id&gt;]` "
        "(e.g. `[mem:abc123]`); skip the citation only when you're "
        "answering from general knowledge that doesn't trace to one of "
        "these chunks.</usage>",
    ]
    for r in results:
        citation = _xml_escape(r.citation)
        chunk_id = _xml_escape(r.chunk.id)
        lines.append(
            f'  <memory id="{chunk_id}" citation="{citation}" relevance="{r.score:.3f}">'
            f"{_xml_escape(r.chunk.text)}</memory>"
        )
    lines.append("</recalled_memories>")
    return "\n".join(lines)


def format_memories_as_prelude(results: list[MemorySearchResult]) -> str:
    """Tight plain-text recall prelude designed to sit ABOVE the base
    system prompt — first thing the model sees.

    Small local models (gemma2/3, qwen2.5:3b, llama3.1:8b) tend to fade
    on long English playbooks, so the recall content needs to be at the
    very top of the prompt in a format that doesn't look like meta-
    documentation. This helper flattens the retrieved chunks into a
    bullet list under a directive header — no XML, no escape sequences,
    no citations. The richer XML block (with chunk_id + citations) is
    still emitted via `format_memories_for_prompt` for citation-aware
    models; this prelude is a redundant "you already know these
    things" reminder placed where attention is highest.
    """
    if not results:
        return ""
    # Positive framing only. Earlier versions used "Never reply 'I don't
    # know'" — small local models (gemma2/3, qwen2.5:3b) sometimes
    # interpret negative meta-instructions as a freeze signal and emit
    # an empty response. The directive below tells the model what TO do
    # without naming the failure mode.
    lines = [
        "## What you already know about this user",
        "",
        "These facts come from your long-term memory and are relevant to",
        "the current question. Use them directly when answering.",
        "",
    ]
    for r in results:
        text = r.chunk.text.strip().replace("\n", " ")
        if len(text) > 280:
            text = text[:280] + "…"
        lines.append(f"- {text}")
    return "\n".join(lines)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
