# Memory system: openclaw ↔ sampyClaw

Side-by-side so we can decide what to actually port. openclaw's memory stack is much larger than the Python stub and the two are **not** modelling the same object — see §Conceptual delta before any port.

## File inventory

| Layer | openclaw | sampyClaw |
|---|---|---|
| Core engine | `src/memory-host-sdk/` (17 top-level + `host/` 40 = **57 ts files** non-test) | — (monolithic) |
| Root-file loader | `src/memory/root-memory-files.ts` (1 file) | — |
| Plug-in entry | `extensions/memory-core/` (**69 ts files** non-test, 39 under `src/memory/`) | *(rolled inline)* |
| Storage backend | `extensions/memory-lancedb/` (**6 files**) | — |
| Wiki source | `extensions/memory-wiki/` (**34 files**) | — |
| Active-memory agent | `extensions/active-memory/` (**1 file**) | — |
| Python impl | — | `sampyclaw/memory/` (**5 modules**: store, retriever, embeddings, tools, models) |

openclaw total ≈ **168 non-test files**. sampyClaw ≈ **5 modules**.

## Conceptual delta (most important)

**openclaw memory = indexed corpus of markdown files on disk.**
- A "memory" is a chunk of a file: `(path, start_line, end_line, hash, text, embedding)`.
- Sources: files in `~/.openclaw/memory/**.md` + optionally `sessions/` transcripts + wiki.
- Search returns snippets with citations (`path:startLine-endLine`).
- Writing = edit the markdown file; engine re-indexes on mtime/hash change.
- Backends: built-in SQLite + FTS5 + embedding column, or the Lancedb plug-in.
- Optional "QMD" query-language for scoped searches (`@scope: path/...`).

**sampyClaw memory = opaque row-per-fact vector store.**
- A "memory" is a single short text: `(id, agent_id, session_key, text, tags, metadata, embedding, created_at)`.
- No file backing. `memory_save` tool appends a row; `memory_search` runs cosine top-k.
- Isolation is per `agent_id` + optional `session_key` (agent-global facts use `session_key IS NULL`).
- `sqlite-vec` for vectors, no FTS, no chunking, no citations.
- Single schema; backend is not pluggable yet.

These are **different products**. openclaw's design is "agent reads your knowledge base and cites from it." sampyClaw's design is "agent writes discrete facts and recalls them later." Don't blindly port — decide first which product sampyClaw should be.

## Feature matrix

| Feature | openclaw | sampyClaw | Port-priority |
|---|---|---|---|
| Vector embeddings | ✅ pluggable provider adapters | ✅ OpenAI-shape HTTP (Ollama default) | — (done) |
| Embedding cache table | ✅ `embedding-cache` table keyed by provider/model/hash | ❌ re-embeds every call | **P1** (cheap win) |
| Multiple providers | ✅ `provider-adapter-registration`, per-model limits | ❌ one client | P1 |
| Dimension lock per DB | ✅ `memory-schema` records dims in `meta` | ✅ read from `memory_vec` SQL | — |
| File-level sync (mtime/hash) | ✅ `files`/`chunks` tables, dirty tracking | ❌ | **Out** unless we adopt file-backed model |
| Chunk-level storage | ✅ `chunks` with path + line ranges | ❌ (text as single blob) | Out unless file-backed |
| FTS5 keyword search | ✅ optional, `unicode61` or `trigram` tokenizer | ❌ | P2 — would help hybrid retrieval |
| Hybrid vector+FTS ranking | ✅ (via QMD engine) | ❌ | P2 |
| MMR diversity re-rank | ✅ `memory/mmr.ts` | ❌ | **P1** (small, improves recall) |
| Temporal decay | ✅ `memory/temporal-decay.ts` | ❌ | P1 |
| Short-term → long-term promotion | ✅ `short-term-promotion.ts` | ❌ | P2 |
| Dreaming / consolidation | ✅ `dreaming*.ts` (phases, narrative, repair, markdown) — 6 files | ❌ | **P2 / maybe skip** — complex, agent-loop dependent |
| Concept vocabulary | ✅ `concept-vocabulary.ts` | ❌ | P2 |
| Citations in results | ✅ `citation: "path:start-end"` | ❌ | Skip (no file backing) |
| Source kinds (memory/sessions) | ✅ `MemorySource = "memory" \| "sessions"` | ❌ (only "memory") | P2 (tie to sessions subsystem) |
| REM-evidence trace | ✅ `rem-evidence.ts` | ❌ | P2 |
| Flush plan (write-back) | ✅ `flush-plan.ts` w/ token thresholds | ❌ | P2 |
| Wiki source ingestion | ✅ `memory-wiki/` obsidian/vault import, claim-health, memory-palace | ❌ | **Out** (wiki is its own product) |
| Lancedb backend | ✅ | ❌ | Out (P2 if scale demands it) |
| `memory_save` tool | ✅ (distinct from `memory_get`/`memory_search`) | ✅ | — |
| `memory_search` tool | ✅ w/ QMD scopes, mode selection | ✅ simple query + k | P2 for QMD |
| `memory_get` tool (read file range) | ✅ | ❌ | Out unless file-backed |
| CLI commands | ✅ `memory-core/src/cli*.ts` (list/sync/dream/stats) | ❌ (`cli/memory_cmd.py` stub) | **P1** (at least sync/list/stats) |
| Prompt-section builder | ✅ `buildPromptSection()` w/ budget | ✅ `format_memories_for_prompt()` | — (done, simpler) |
| Agent auto-injection | ✅ via prompt-section + flush-plan | Partial — formatter exists, not wired into `LocalAgent` / `PiAgent` system-prompt assembly yet | **P0 for Phase A.1** |
| Per-agent isolation | ✅ `agent-scope.ts` (`resolveAgentDir`) | ✅ `agent_id` FK | — |
| Session-scope memories | ✅ sessions source | ✅ `session_key` column | — |
| Dimension mismatch error | ✅ | ✅ explicit ValueError | — |
| Status endpoint | ✅ `MemoryProviderStatus` | ❌ (no `gateway/memory_methods.py` coverage yet) | **P1** |
| Reindex / atomic reindex | ✅ `manager-atomic-reindex.ts`, `manager-reindex-state.ts` | ❌ | P1 (needed if we change embedding model) |
| Async / batch state machines | ✅ `manager-async-state.ts`, `manager-batch-state.ts`, batch-runner/-upload/-status | ❌ | Out (avoid unless throughput forces it) |
| HTTP remote embeddings client | ✅ `embeddings-remote-*.ts` | ~ partial (our single OpenAI client) | P2 |
| Node-llama local embeddings | ✅ `node-llama.ts` | ❌ (use Ollama) | Skip — stack-specific |
| Secret input for API keys | ✅ `secret-input.ts` | ❌ (env var only) | P1 (coupled to `config/credentials.py`) |

## Schema-level diff

openclaw `memory-schema.ts` builds four tables:

```
meta           (key,value)
files          (path, source, hash, mtime, size)
chunks         (id, path, source, start_line, end_line, hash, model, text, embedding, updated_at)
embedding_cache (provider, model, provider_key, hash, embedding, dims, updated_at)
+ FTS5 shadow table on chunks.text
```

sampyClaw `store.py` builds two:

```
memories  (id, agent_id, session_key, text, tags, metadata, created_at)
memory_vec (VIRTUAL vec0: memory_id, embedding float[dim] cosine)
```

Column-level gaps to close if we keep current design:
- No `embedding_cache` → re-embed cost on every `memory_save`.
- No FTS shadow → no keyword fallback when embeddings are noisy.
- No `meta` table → no place to record schema version for migrations.

## Design decisions — locked

Recorded 2026-04-25.

1. **Memory unit: chunk-of-file (openclaw model).** `sampyclaw/memory/` will be rebuilt around markdown files on disk at `~/.sampyclaw/memory/**.md` (mirrors openclaw's `~/.openclaw/memory/`). A memory = `(path, start_line, end_line, hash, text, embedding)`. Writing a memory = editing the markdown file; engine re-indexes on mtime/hash change. Current row-of-fact store in `sampyclaw/memory/store.py` is superseded — kept only until the new pipeline lands, then deleted.
2. **Source pluralisation:** port the `"memory" | "sessions"` distinction from openclaw. Session transcripts will be indexed separately from the memory corpus so `memory_search` can scope or mix.
3. **Backend plugin boundary:** mirror `memory-core` as a plug-in shape (`sampyclaw/extensions/memory_core/` or similar) so a future Lancedb-equivalent can register through `plugin_sdk`. v1 ships only the sqlite backend; the seam is what matters.
4. **Dreaming / consolidation:** still deferred for v1. Design the schema so it can be layered later (keep `sessions` source separate; add a `dreams` table stub in `meta` without a producer).

## Port plan for memory (chunk-of-file)

Ordered. Each step must land green before the next. Phase labels refer to `docs/PORTING_PLAN.md`.

1. **(P0 / A.1)** **Schema rewrite.** Replace `store.py` schema with openclaw's: `meta`, `files`, `chunks`, `embedding_cache`, + FTS5 shadow on `chunks.text`. Record `schema_version`, `embedding_model`, `dims` in `meta`. Keep sqlite-vec for the ANN side (the vec0 virtual table becomes the embedding index over `chunks.id`).
2. **(P0 / A.1)** **File walker + chunker.** Port `memory-host-sdk/host/session-files.ts` + `read-file.ts` + chunking logic. Walk `memory_dir`, hash files, diff against `files` table, re-chunk and re-embed only dirty files. Configurable chunk size/overlap.
3. **(P0 / A.1)** **Embedding cache.** Port `embedding_cache` table (`provider, model, provider_key, hash → embedding`) so re-runs don't re-embed unchanged text.
4. **(P0 / A.1)** **Search path.** Replace `store.search()` with a chunk-level top-k + citation (`path:start-end`). Update `MemoryRetriever` + `format_memories_for_prompt()` to render citations in the system prompt.
5. **(P0 / A.1)** **Tool surface.** Rework `memory_save` tool: instead of inserting a row, it appends to a single "inbox" markdown file (`~/.sampyclaw/memory/inbox.md`) and triggers an incremental reindex on that file. Add `memory_get` tool that reads a file range by citation (mirrors openclaw `MemoryReadResult`). Keep `memory_search` but return the new chunk shape.
6. **(P1)** **Sessions source.** Wire session transcripts as a second `source="sessions"` feed into `files`/`chunks`. Pull transcript paths from the sessions subsystem (once it lands) or from a configured dir.
7. **(P1)** **MMR diversity re-rank** — port `memory/mmr.ts`. Wire as retriever option.
8. **(P1)** **Temporal-decay re-rank** — `score = cos_sim * exp(-λ·age)`. Opt-in.
9. **(P1)** **FTS5 hybrid ranking.** Use the shadow table for BM25 keyword path and blend with cosine + MMR. This is openclaw's "builtin" search mode.
10. **(P1)** **Reindex command.** `sampyclaw memory rebuild` — atomic reindex via temp DB + swap (mirror `manager-atomic-reindex.ts`). Required when `embedding_model` changes.
11. **(P1)** **Memory CLI.** Finish `cli/memory_cmd.py` — `list`, `sync`, `stats`, `rebuild`, `get PATH:L1-L2`.
12. **(P1)** **Gateway surface.** Flesh out `gateway/memory_methods.py` for list/search/status/sync so UI clients can drive the engine.
13. **(P1)** **Plugin boundary.** Move the concrete backend under `extensions/memory_core/` behind a `MemoryBackend` Protocol; wire discovery through `plugin_sdk`.
14. **(P1)** **Provider adapters.** Port `provider-adapter-registration` seam so multiple embedding providers (Ollama, OpenAI, Voyage) can register with per-model dimension/limit metadata.
15. **(P2)** **Secret input for API keys** — integrate with `config/credentials.py`.
16. **(P2)** **QMD query language** — port parser + scope resolution (`host/qmd-*.ts`).
17. **(P2 / maybe skip)** dreaming/consolidation — revisit only after items 1–15 are in use.

## Still not porting

- `memory-wiki/` (Obsidian vault, memory-palace, claim-health) — out of v1 scope.
- `memory-lancedb/` — only after step 13's plugin boundary exists and someone actually hits sqlite's N limit.
- `node-llama` local embedding provider — stack-specific; Ollama covers the same need.
- Batch upload/status/runner (`batch-*.ts`) — defer until throughput forces it.

## Migration impact on current code

Current `sampyclaw/memory/` (row-of-fact) must be treated as a v0 throwaway once step 1–5 land:

- `store.py` — schema replaced wholesale.
- `retriever.py` — signature stable (`save`/`search`/`format_memories_for_prompt`) but internals rewritten; `save()` now edits a markdown file instead of inserting a row.
- `embeddings.py` — kept; becomes one of several provider adapters.
- `models.py` — `MemoryItem` replaced with `MemoryChunk` + `MemorySearchResult` mirroring openclaw's `host/types.ts`.
- `tools.py` — `memory_save` semantics change; add `memory_get`.

Existing tests covering the row-of-fact flow (`tests/test_memory*`) will be deleted and rewritten against the chunk-of-file model. Confirm this is acceptable before step 1 — the 429-green count will drop during the rewrite window.
