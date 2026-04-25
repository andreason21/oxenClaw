"""Memory data models — chunk-of-file model.

Mirrors openclaw `src/memory-host-sdk/host/types.ts`. A memory is a chunk of
a markdown file on disk identified by `(path, start_line, end_line, hash)`
plus a vector embedding stored elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemorySource = Literal["memory", "sessions"]


@dataclass(frozen=True)
class MemoryChunk:
    """One indexed slice of a markdown file."""

    id: str
    path: str
    source: str
    start_line: int
    end_line: int
    text: str
    hash: str


@dataclass(frozen=True)
class MemorySearchResult:
    """A retrieved chunk with similarity score."""

    chunk: MemoryChunk
    score: float
    distance: float

    @property
    def citation(self) -> str:
        return f"{self.chunk.path}:{self.chunk.start_line}-{self.chunk.end_line}"


@dataclass(frozen=True)
class MemoryReadResult:
    """A slice of a memory file returned by the read-file tool."""

    path: str
    text: str
    start_line: int
    end_line: int
    truncated: bool
    next_from: int | None


@dataclass(frozen=True)
class FileEntry:
    """Row from the `files` table joined with chunk count."""

    path: str
    source: str
    hash: str
    mtime: float
    size: int
    chunk_count: int


@dataclass(frozen=True)
class SyncReport:
    """Summary returned by `MemoryIndexer.sync`."""

    added: int
    changed: int
    deleted: int
    chunks_embedded: int
    cache_hits: int
