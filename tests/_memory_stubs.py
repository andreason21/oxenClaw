"""Test helpers for the memory subsystem.

Deterministic hash-based embedder so tests do not hit Ollama.
"""

from __future__ import annotations

import hashlib
import struct


class StubEmbeddings:
    """16-dim deterministic embedder. Counts calls for cache assertions."""

    def __init__(self, dims: int = 16, model: str = "stub-model") -> None:
        self._dims = dims
        self._model = model
        self.call_count = 0
        self.total_texts = 0

    @property
    def dimensions(self) -> int:
        return self._dims

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "stub"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        self.total_texts += len(texts)
        out: list[list[float]] = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            # Repeat/truncate to fill dims.
            buf = (digest * ((self._dims * 4 // len(digest)) + 1))[: self._dims * 4]
            vec = list(struct.unpack(f"{self._dims}f", buf))
            # Normalise so cosine works: scale by 1/(1 + |v|).
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out

    async def aclose(self) -> None:
        return None
