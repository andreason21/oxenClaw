"""Long-term memory: chunk-of-file corpus + vector + FTS retrieval.

Layout mirrors openclaw `src/memory-host-sdk/`. Storage is SQLite +
`sqlite-vec` + FTS5; embeddings come from an OpenAI-compatible
`/v1/embeddings` endpoint (Ollama by default).
"""

from sampyclaw.memory.embedding_cache import EmbeddingCache
from sampyclaw.memory.embeddings import (
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_EMBED_MODEL,
    EmbeddingError,
    EmbeddingProvider,
    OpenAIEmbeddings,
)
from sampyclaw.memory.indexer import MemoryIndexer
from sampyclaw.memory.models import (
    FileEntry,
    MemoryChunk,
    MemoryReadResult,
    MemorySearchResult,
    MemorySource,
    SyncReport,
)
from sampyclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from sampyclaw.memory.store import MemoryStore
from sampyclaw.memory.tools import (
    memory_get_tool,
    memory_save_tool,
    memory_search_tool,
)

__all__ = [
    "DEFAULT_EMBED_BASE_URL",
    "DEFAULT_EMBED_MODEL",
    "EmbeddingCache",
    "EmbeddingError",
    "EmbeddingProvider",
    "FileEntry",
    "MemoryChunk",
    "MemoryIndexer",
    "MemoryReadResult",
    "MemoryRetriever",
    "MemorySearchResult",
    "MemorySource",
    "MemoryStore",
    "OpenAIEmbeddings",
    "SyncReport",
    "format_memories_for_prompt",
    "memory_get_tool",
    "memory_save_tool",
    "memory_search_tool",
]
