"""memory.* JSON-RPC methods bound to a MemoryRetriever."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sampyclaw.gateway.router import Router
from sampyclaw.memory.embeddings import EmbeddingError
from sampyclaw.memory.hybrid import HybridConfig
from sampyclaw.memory.mmr import MMRConfig
from sampyclaw.memory.models import (
    FileEntry,
    MemoryChunk,
    MemoryReadResult,
    MemorySearchResult,
    SyncReport,
)
from sampyclaw.memory.retriever import MemoryRetriever
from sampyclaw.memory.temporal_decay import TemporalDecayConfig


class _HybridParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    vector_weight: float = 0.7
    text_weight: float = 0.3
    candidate_multiplier: int = 3

    def to_config(self) -> HybridConfig:
        return HybridConfig(
            enabled=self.enabled,
            vector_weight=self.vector_weight,
            text_weight=self.text_weight,
            candidate_multiplier=self.candidate_multiplier,
        )


class _MMRParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    lambda_: float = Field(default=0.7, alias="lambda")

    def to_config(self) -> MMRConfig:
        return MMRConfig(enabled=self.enabled, lambda_=self.lambda_)


class _DecayParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    half_life_days: float = 30.0

    def to_config(self) -> TemporalDecayConfig:
        return TemporalDecayConfig(
            enabled=self.enabled,
            half_life_days=self.half_life_days,
        )


class _SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    k: int = 5
    source: str | None = None
    hybrid: _HybridParams | None = None
    mmr: _MMRParams | None = None
    temporal_decay: _DecayParams | None = None


class _SyncParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StatsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = None


class _GetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    from_line: int = 1
    lines: int = 120


class _SaveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    tags: list[str] = Field(default_factory=list)


def _serialise_chunk(c: MemoryChunk) -> dict[str, Any]:
    return {
        "id": c.id,
        "path": c.path,
        "source": c.source,
        "start_line": c.start_line,
        "end_line": c.end_line,
        "text": c.text,
        "hash": c.hash,
    }


def _serialise_hit(h: MemorySearchResult) -> dict[str, Any]:
    return {
        "chunk": _serialise_chunk(h.chunk),
        "score": h.score,
        "distance": h.distance,
        "citation": h.citation,
    }


def _serialise_file(f: FileEntry) -> dict[str, Any]:
    return {
        "path": f.path,
        "source": f.source,
        "hash": f.hash,
        "mtime": f.mtime,
        "size": f.size,
        "chunk_count": f.chunk_count,
    }


def _serialise_report(r: SyncReport) -> dict[str, Any]:
    return {
        "added": r.added,
        "changed": r.changed,
        "deleted": r.deleted,
        "chunks_embedded": r.chunks_embedded,
        "cache_hits": r.cache_hits,
    }


def _serialise_read(r: MemoryReadResult) -> dict[str, Any]:
    return {
        "path": r.path,
        "text": r.text,
        "start_line": r.start_line,
        "end_line": r.end_line,
        "truncated": r.truncated,
        "next_from": r.next_from,
    }


def register_memory_methods(router: Router, retriever: MemoryRetriever) -> None:
    @router.method("memory.search", _SearchParams)
    async def _search(p: _SearchParams) -> dict[str, Any]:
        try:
            hits = await retriever.search(
                p.query,
                k=p.k,
                source=p.source,
                hybrid=p.hybrid.to_config() if p.hybrid else None,
                mmr=p.mmr.to_config() if p.mmr else None,
                temporal_decay=(
                    p.temporal_decay.to_config() if p.temporal_decay else None
                ),
            )
        except EmbeddingError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "hits": [_serialise_hit(h) for h in hits]}

    @router.method("memory.sync", _SyncParams)
    async def _sync(_: _SyncParams) -> dict[str, Any]:
        try:
            report = await retriever.sync()
        except EmbeddingError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "report": _serialise_report(report)}

    @router.method("memory.stats", _StatsParams)
    async def _stats(_: _StatsParams) -> dict[str, Any]:
        store = retriever.store
        return {
            "ok": True,
            "total_files": store.count_files(),
            "total_chunks": store.count_chunks(),
            "dimensions": store.dimensions,
            "path": str(store.path),
            "meta": store.read_meta(),
        }

    @router.method("memory.list", _ListParams)
    async def _list(p: _ListParams) -> dict[str, Any]:
        files = retriever.store.list_files(source=p.source)
        return {"ok": True, "files": [_serialise_file(f) for f in files]}

    @router.method("memory.get", _GetParams)
    async def _get(p: _GetParams) -> dict[str, Any]:
        try:
            result = retriever.get(p.path, from_line=p.from_line, lines=p.lines)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "read": _serialise_read(result)}

    @router.method("memory.save", _SaveParams)
    async def _save(p: _SaveParams) -> dict[str, Any]:
        try:
            report = await retriever.save(p.text, tags=p.tags or None)
        except EmbeddingError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "report": _serialise_report(report)}
