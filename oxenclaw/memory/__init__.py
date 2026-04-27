"""Long-term memory: chunk-of-file corpus + vector + FTS retrieval.

Layout mirrors openclaw `src/memory-host-sdk/`. Storage is SQLite +
`sqlite-vec` + FTS5; embeddings come from an OpenAI-compatible
`/v1/embeddings` endpoint (Ollama by default).
"""

from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.embeddings import (
    DEFAULT_EMBED_BASE_URL,
    DEFAULT_EMBED_MODEL,
    AnthropicEmbeddings,
    CohereEmbeddings,
    Embedder,
    EmbeddingError,
    EmbeddingProvider,
    OpenAIEmbeddings,
    UnknownEmbedderProvider,
    build_embedder,
)
from oxenclaw.memory.indexer import MemoryIndexer
from oxenclaw.memory.provider import (
    BuiltinMemoryProvider,
    MemoryProvider,
    MemoryProviderRegistry,
)
from oxenclaw.memory.models import (
    FileEntry,
    MemoryChunk,
    MemoryReadResult,
    MemorySearchResult,
    MemorySource,
    SyncReport,
)
from oxenclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from oxenclaw.memory.store import MemoryStore
from oxenclaw.memory.tools import (
    memory_get_tool,
    memory_save_tool,
    memory_search_tool,
)

__all__ = [
    "DEFAULT_EMBED_BASE_URL",
    "DEFAULT_EMBED_MODEL",
    "AnthropicEmbeddings",
    "BuiltinMemoryProvider",
    "CohereEmbeddings",
    "Embedder",
    "EmbeddingCache",
    "EmbeddingError",
    "EmbeddingProvider",
    "FileEntry",
    "MemoryChunk",
    "MemoryIndexer",
    "MemoryProvider",
    "MemoryProviderRegistry",
    "MemoryReadResult",
    "MemoryRetriever",
    "MemorySearchResult",
    "MemorySource",
    "MemoryStore",
    "OpenAIEmbeddings",
    "SyncReport",
    "UnknownEmbedderProvider",
    "build_embedder",
    "format_memories_for_prompt",
    "memory_get_tool",
    "memory_save_tool",
    "memory_search_tool",
]
