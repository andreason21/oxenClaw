"""Cache layer wrapping an `EmbeddingProvider`.

Cache lookup keyed by `(provider, model, sha256(text))`. On a miss the
underlying provider is called for just the missing items and the cache
is populated. The `MemoryStore` owns the sqlite connection so the
embedding cache shares one DB file with the rest of the schema.
"""

from __future__ import annotations

from oxenclaw.memory.embeddings import EmbeddingProvider
from oxenclaw.memory.hashing import sha256_text
from oxenclaw.memory.store import MemoryStore


class EmbeddingCache:
    """Read-through cache around an `EmbeddingProvider`."""

    def __init__(self, provider: EmbeddingProvider, store: MemoryStore) -> None:
        self._provider = provider
        self._store = store
        self._cache_hits = 0

    @property
    def provider(self) -> str:
        return self._provider.provider_name

    @property
    def model(self) -> str:
        return self._provider.model

    @property
    def dimensions(self) -> int:
        return self._provider.dimensions

    @property
    def cache_hits(self) -> int:
        return self._cache_hits

    @property
    def underlying(self) -> EmbeddingProvider:
        return self._provider

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings, hitting cache where possible.

        Resets `cache_hits` to count hits for this call only.
        """
        if not texts:
            self._cache_hits = 0
            return []

        out: list[list[float] | None] = [None] * len(texts)
        misses_idx: list[int] = []
        misses_text: list[str] = []
        misses_hash: list[str] = []
        hits = 0
        for i, t in enumerate(texts):
            h = sha256_text(t)
            cached = self._store.cache_get(self.provider, self.model, h)
            if cached is not None:
                out[i] = cached
                hits += 1
            else:
                misses_idx.append(i)
                misses_text.append(t)
                misses_hash.append(h)

        if misses_text:
            fresh = await self._provider.embed(misses_text)
            put_batch: list[tuple[str, list[float]]] = []
            for idx, h, vec in zip(misses_idx, misses_hash, fresh, strict=True):
                out[idx] = vec
                put_batch.append((h, vec))
            self._store.cache_put_many(self.provider, self.model, put_batch)

        self._cache_hits = hits
        # All slots filled by construction.
        return [v for v in out if v is not None]
