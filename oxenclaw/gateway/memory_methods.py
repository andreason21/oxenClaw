"""memory.* JSON-RPC methods bound to a MemoryRetriever."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.gateway.router import Router
from oxenclaw.memory.embeddings import EmbeddingError
from oxenclaw.memory.hybrid import HybridConfig
from oxenclaw.memory.mmr import MMRConfig
from oxenclaw.memory.models import (
    FileEntry,
    MemoryChunk,
    MemoryReadResult,
    MemorySearchResult,
    SyncReport,
)
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.temporal_decay import TemporalDecayConfig


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


class _DeleteParams(BaseModel):
    """Delete by either chunk id or file path. Path-based removes
    every chunk under that path (mirrors openclaw's `memory.delete`)."""

    model_config = ConfigDict(extra="forbid")
    chunk_id: str | None = None
    path: str | None = None


class _ExportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str | None = None  # filter to one corpus, or None for all


class _ImportParams(BaseModel):
    """Bulk import — same JSON shape as `memory.export`. Files re-
    inserted via raw inbox path (so they get re-embedded on next
    sync). For full-fidelity round-trip including vectors, use the
    archive tool instead."""

    model_config = ConfigDict(extra="forbid")
    chunks: list[dict[str, Any]] = Field(default_factory=list)
    files: list[dict[str, Any]] = Field(default_factory=list)
    overwrite: bool = False


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
                temporal_decay=(p.temporal_decay.to_config() if p.temporal_decay else None),
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

    @router.method("memory.delete", _DeleteParams)
    async def _delete(p: _DeleteParams) -> dict[str, Any]:
        if not p.chunk_id and not p.path:
            return {"ok": False, "error": "either chunk_id or path is required"}
        store = retriever.store
        if p.path:
            store.delete_file(p.path)
            return {"ok": True, "deleted_path": p.path}
        # chunk_id branch — fetch first, then drop just the chunk
        chunk = store.get_chunk(p.chunk_id)
        if chunk is None:
            return {"ok": False, "error": f"no chunk with id {p.chunk_id!r}"}
        # Reuse the file-level delete when a chunk is the only one for its
        # path; otherwise issue chunk-targeted DELETEs across the three
        # tables (mirrors `delete_file` minus the file row).
        conn = store.conn
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (p.chunk_id,))
        conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (p.chunk_id,))
        conn.execute("DELETE FROM chunks WHERE id = ?", (p.chunk_id,))
        conn.commit()
        return {"ok": True, "deleted_chunk_id": p.chunk_id}

    @router.method("memory.export", _ExportParams)
    async def _export(p: _ExportParams) -> dict[str, Any]:
        """Round-trip JSON dump: chunks + file metadata.

        Vectors are NOT included — they're regenerated by `memory.sync`
        on the import side from the chunk text + the configured
        embedder. This keeps the export embedder-agnostic at the cost
        of a re-embed on import.
        """
        store = retriever.store
        files = store.list_files(source=p.source)
        chunks_out: list[dict[str, Any]] = []
        for f in files:
            for row in store.conn.execute(
                """
                SELECT id, path, source, start_line, end_line, hash, text, model
                FROM chunks
                WHERE path = ?
                ORDER BY start_line ASC
                """,
                (f.path,),
            ).fetchall():
                chunks_out.append(
                    {
                        "id": row["id"],
                        "path": row["path"],
                        "source": row["source"],
                        "start_line": int(row["start_line"]),
                        "end_line": int(row["end_line"]),
                        "hash": row["hash"],
                        "text": row["text"],
                        "model": row["model"],
                    }
                )
        return {
            "ok": True,
            "schema_version": 1,
            "filter": {"source": p.source},
            "files": [_serialise_file(f) for f in files],
            "chunks": chunks_out,
        }

    @router.method("memory.import", _ImportParams)
    async def _import(p: _ImportParams) -> dict[str, Any]:
        """Bulk insert previously-exported chunks. Vectors are NOT
        carried in the JSON; the caller should run `memory.sync()`
        afterwards to regenerate them. We insert only the rows the
        downstream sync flow needs to recognise paths as already-known.
        """
        store = retriever.store
        conn = store.conn
        imported_files = 0
        imported_chunks = 0
        for f in p.files:
            try:
                if not p.overwrite:
                    existing = conn.execute(
                        "SELECT 1 FROM files WHERE path = ?", (f["path"],)
                    ).fetchone()
                    if existing:
                        continue
                store.upsert_file(
                    path=f["path"],
                    source=f["source"],
                    hash_=f["hash"],
                    mtime=float(f.get("mtime", 0.0)),
                    size=int(f.get("size", 0)),
                )
                imported_files += 1
            except (KeyError, TypeError):
                continue
        for c in p.chunks:
            try:
                if not p.overwrite:
                    existing = conn.execute(
                        "SELECT 1 FROM chunks WHERE id = ?", (c["id"],)
                    ).fetchone()
                    if existing:
                        continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO chunks
                        (id, path, source, start_line, end_line, hash, text, model, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
                    """,
                    (
                        c["id"], c["path"], c["source"],
                        int(c["start_line"]), int(c["end_line"]),
                        c["hash"], c["text"], c.get("model"),
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO chunks_fts(chunk_id, text, path, source) VALUES (?, ?, ?, ?)",
                    (c["id"], c["text"], c["path"], c["source"]),
                )
                imported_chunks += 1
            except (KeyError, TypeError):
                continue
        conn.commit()
        return {
            "ok": True,
            "imported_files": imported_files,
            "imported_chunks": imported_chunks,
            "note": "Vectors not imported — run `memory.sync` to regenerate embeddings.",
        }
