"""Walk corpus dirs → diff against `files` table → re-chunk + re-embed.

Sync is incremental: unchanged files (matching mtime + hash) are skipped.
"""

from __future__ import annotations

from pathlib import Path

from sampyclaw.memory.chunker import chunk_markdown
from sampyclaw.memory.embedding_cache import EmbeddingCache
from sampyclaw.memory.hashing import sha256_text
from sampyclaw.memory.models import SyncReport
from sampyclaw.memory.store import MemoryStore
from sampyclaw.memory.walker import scan_memory_dir


class MemoryIndexer:
    """Glue between filesystem walker, chunker, embeddings, and store."""

    def __init__(
        self,
        store: MemoryStore,
        embeddings_cache: EmbeddingCache,
        memory_dir: Path,
        *,
        chunker_opts: dict[str, int] | None = None,
    ) -> None:
        self._store = store
        self._embeddings = embeddings_cache
        self._memory_dir = memory_dir
        self._chunker_opts = chunker_opts or {}

    async def sync(
        self, sources: dict[str, Path] | None = None
    ) -> SyncReport:
        srcs = sources or {"memory": self._memory_dir}
        added = 0
        changed = 0
        deleted = 0
        chunks_embedded = 0
        cache_hits = 0
        meta_done = False

        for source, root in srcs.items():
            existing = {f.path: f for f in self._store.list_files(source=source)}
            seen: set[str] = set()
            for relpath, _src, mtime, size, content_hash, text in scan_memory_dir(
                root, source=source
            ):
                seen.add(relpath)
                prev = existing.get(relpath)
                if prev is not None and prev.hash == content_hash:
                    # Unchanged: refresh mtime/size in case fs metadata moved.
                    if prev.mtime != mtime or prev.size != size:
                        self._store.upsert_file(
                            relpath, source, content_hash, mtime, size
                        )
                    continue

                pieces = chunk_markdown(text, **self._chunker_opts)
                if not pieces:
                    if prev is not None:
                        self._store.delete_file(relpath)
                        deleted += 1
                    continue
                texts = [t for _, _, t in pieces]
                vectors = await self._embeddings.embed(texts)
                cache_hits += self._embeddings.cache_hits
                chunks_embedded += len(texts) - self._embeddings.cache_hits

                if not meta_done:
                    self._store.ensure_schema_meta(
                        provider=self._embeddings.provider,
                        model=self._embeddings.model,
                        dims=len(vectors[0]),
                    )
                    meta_done = True

                self._store.upsert_file(
                    relpath, source, content_hash, mtime, size
                )
                chunk_rows: list[tuple[int, int, str, str, list[float]]] = [
                    (start, end, t, sha256_text(t), vec)
                    for (start, end, t), vec in zip(pieces, vectors, strict=True)
                ]
                self._store.replace_chunks_for_file(
                    relpath, source, self._embeddings.model, chunk_rows
                )
                if prev is None:
                    added += 1
                else:
                    changed += 1

            for path in existing.keys() - seen:
                self._store.delete_file(path)
                deleted += 1

        return SyncReport(
            added=added,
            changed=changed,
            deleted=deleted,
            chunks_embedded=chunks_embedded,
            cache_hits=cache_hits,
        )
