"""SQLite + sqlite-vec + FTS5 backed chunk store.

Schema mirrors openclaw `host/memory-schema.ts`:

  meta               (key, value)
  files              (path PK, source, hash, mtime, size)
  chunks             (id PK, path FK, source, start_line, end_line, hash,
                      text, model, updated_at)
  chunks_vec         vec0 virtual table (chunk_id PK, embedding cosine)
  chunks_fts         FTS5 external-content table mirroring chunks.text
  embedding_cache    (provider, model, content_hash, embedding BLOB,
                      dims, updated_at)  -- composite PK

The vec0 table is sized on first insert; the dim is recorded in `meta` and
must match on every subsequent run.
"""

from __future__ import annotations

import sqlite3
import struct
import time
import uuid
from pathlib import Path

import sqlite_vec

from oxenclaw.memory.hybrid import bm25_rank_to_score
from oxenclaw.memory.models import FileEntry, MemoryChunk
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.store")

SCHEMA_VERSION = "1"


def _encode_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def new_chunk_id() -> str:
    return uuid.uuid4().hex


class MemoryStore:
    """SQLite-backed chunk store with vector + keyword indexes."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._dim: int | None = None
        self._conn = self._open()
        self._init_schema()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conn(self) -> sqlite3.Connection:
        """Underlying connection, shared with the embedding cache."""
        return self._conn

    @property
    def dimensions(self) -> int | None:
        return self._dim

    def close(self) -> None:
        self._conn.close()

    def _open(self) -> sqlite3.Connection:
        # `check_same_thread=False` lets the indexer thread + the gateway
        # request loop share one connection. WAL mode lets readers (search)
        # run while a writer (indexer) holds the write lock — without WAL,
        # any commit blocks every concurrent SELECT.
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL is the standard pairing with WAL — durability is preserved
        # across application crashes; only a host-level power-loss can lose
        # the last commit. Big throughput win for write-heavy indexing.
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
              path TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              hash TEXT NOT NULL,
              mtime REAL NOT NULL,
              size INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
              id TEXT PRIMARY KEY,
              path TEXT NOT NULL,
              source TEXT NOT NULL,
              start_line INTEGER NOT NULL,
              end_line INTEGER NOT NULL,
              hash TEXT NOT NULL,
              text TEXT NOT NULL,
              model TEXT NOT NULL,
              updated_at REAL NOT NULL,
              FOREIGN KEY(path) REFERENCES files(path) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
            CREATE INDEX IF NOT EXISTS chunks_source_idx ON chunks(source);
            CREATE TABLE IF NOT EXISTS embedding_cache (
              provider TEXT NOT NULL,
              model TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              embedding BLOB NOT NULL,
              dims INTEGER NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (provider, model, content_hash)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
              text,
              chunk_id UNINDEXED,
              path UNINDEXED,
              source UNINDEXED
            );
            -- M-6: curated tier. Inbox is raw; `short_term` is the
            -- promoted store with confidence + tags + spaced-repetition
            -- review schedule. Promote = "this fact is durable enough
            -- to surface in retrieval-time recall ahead of raw inbox".
            CREATE TABLE IF NOT EXISTS short_term (
              id TEXT PRIMARY KEY,
              source_chunk_id TEXT,           -- chunk we promoted from (null for direct adds)
              text TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '',  -- comma-joined tags
              confidence REAL NOT NULL DEFAULT 0.5,
              promoted_at REAL NOT NULL,
              last_reviewed_at REAL,
              review_count INTEGER NOT NULL DEFAULT 0,
              archived_at REAL                -- soft-delete marker
            );
            CREATE INDEX IF NOT EXISTS short_term_active_idx
              ON short_term(archived_at) WHERE archived_at IS NULL;
            """
        )
        # Detect dim if vec table already exists.
        row = c.execute("SELECT sql FROM sqlite_master WHERE name='chunks_vec'").fetchone()
        if row is not None and "float[" in row["sql"].lower():
            sql = row["sql"]
            try:
                bracket = sql.split("loat[", 1)[1]
                self._dim = int(bracket.split("]", 1)[0])
            except (IndexError, ValueError):
                self._dim = None
        c.commit()

    # ── meta ──

    def ensure_schema_meta(self, provider: str, model: str, dims: int) -> None:
        existing = self.read_meta()
        if "dims" in existing:
            current_dims = int(existing["dims"])
            if current_dims != dims:
                raise ValueError(
                    f"embedding dimension mismatch: store was built for "
                    f"{current_dims} but provider returned {dims}. Run "
                    f"`oxenclaw memory rebuild --yes` to reset the index."
                )
        if "embedding_model" in existing and existing["embedding_model"] != model:
            raise ValueError(
                f"embedding model mismatch: store recorded "
                f"{existing['embedding_model']!r} but got {model!r}. Run "
                f"`oxenclaw memory rebuild --yes` to reset the index."
            )
        if "embedding_provider" in existing and existing["embedding_provider"] != provider:
            raise ValueError(
                f"embedding provider mismatch: store recorded "
                f"{existing['embedding_provider']!r} but got {provider!r}. "
                f"Run `oxenclaw memory rebuild --yes` to reset the index."
            )
        now = time.time()
        rows = [
            ("schema_version", SCHEMA_VERSION),
            ("embedding_provider", provider),
            ("embedding_model", model),
            ("dims", str(dims)),
        ]
        if "created_at" not in existing:
            rows.append(("created_at", str(now)))
        self._conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", rows)
        self._conn.commit()
        self._ensure_vec_table(dims)

    def read_meta(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def _ensure_vec_table(self, dim: int) -> None:
        if self._dim is not None:
            if self._dim != dim:
                raise ValueError(
                    f"embedding dimension mismatch: vec table built for "
                    f"{self._dim}, requested {dim}"
                )
            return
        self._conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
              chunk_id TEXT PRIMARY KEY,
              embedding float[{dim}] distance_metric=cosine
            )
            """
        )
        self._conn.commit()
        self._dim = dim

    # ── files ──

    def upsert_file(self, path: str, source: str, hash_: str, mtime: float, size: int) -> None:
        self._conn.execute(
            """
            INSERT INTO files (path, source, hash, mtime, size)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              source=excluded.source,
              hash=excluded.hash,
              mtime=excluded.mtime,
              size=excluded.size
            """,
            (path, source, hash_, mtime, size),
        )
        self._conn.commit()

    def delete_file(self, path: str) -> None:
        chunk_ids = [
            r["id"]
            for r in self._conn.execute("SELECT id FROM chunks WHERE path = ?", (path,)).fetchall()
        ]
        if chunk_ids:
            qmarks = ",".join("?" * len(chunk_ids))
            self._conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({qmarks})", chunk_ids)
            self._conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({qmarks})", chunk_ids)
        self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def list_files(self, source: str | None = None) -> list[FileEntry]:
        sql = """
            SELECT f.path, f.source, f.hash, f.mtime, f.size,
                   COALESCE(cnt.n, 0) AS chunk_count
            FROM files f
            LEFT JOIN (
              SELECT path, COUNT(*) AS n FROM chunks GROUP BY path
            ) cnt ON cnt.path = f.path
        """
        args: list[object] = []
        if source is not None:
            sql += " WHERE f.source = ?"
            args.append(source)
        sql += " ORDER BY f.path"
        rows = self._conn.execute(sql, args).fetchall()
        return [
            FileEntry(
                path=r["path"],
                source=r["source"],
                hash=r["hash"],
                mtime=float(r["mtime"]),
                size=int(r["size"]),
                chunk_count=int(r["chunk_count"]),
            )
            for r in rows
        ]

    def count_files(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()
        return int(row["n"])

    def count_chunks(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        return int(row["n"])

    # ── chunks ──

    def replace_chunks_for_file(
        self,
        path: str,
        source: str,
        model: str,
        chunks: list[tuple[int, int, str, str, list[float]]],
    ) -> list[str]:
        """Atomically delete + reinsert chunks for `path`. Returns new chunk ids."""
        if chunks:
            self._ensure_vec_table(len(chunks[0][4]))
        new_ids: list[str] = []
        now = time.time()
        try:
            self._conn.execute("BEGIN")
            existing = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM chunks WHERE path = ?", (path,)
                ).fetchall()
            ]
            if existing:
                qmarks = ",".join("?" * len(existing))
                self._conn.execute(f"DELETE FROM chunks_vec WHERE chunk_id IN ({qmarks})", existing)
                self._conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({qmarks})", existing)
            self._conn.execute("DELETE FROM chunks WHERE path = ?", (path,))
            for start_line, end_line, text, hash_, embedding in chunks:
                cid = new_chunk_id()
                new_ids.append(cid)
                self._conn.execute(
                    """
                    INSERT INTO chunks (id, path, source, start_line, end_line,
                                        hash, text, model, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid, path, source, start_line, end_line, hash_, text, model, now),
                )
                self._conn.execute(
                    "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (cid, _encode_vector(embedding)),
                )
                self._conn.execute(
                    """
                    INSERT INTO chunks_fts (text, chunk_id, path, source)
                    VALUES (?, ?, ?, ?)
                    """,
                    (text, cid, path, source),
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return new_ids

    def get_chunk(self, chunk_id: str) -> MemoryChunk | None:
        row = self._conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return _row_to_chunk(row) if row else None

    def search_vector(
        self,
        query_embedding: list[float],
        k: int,
        source: str | None = None,
    ) -> list[tuple[MemoryChunk, float]]:
        if k <= 0 or self._dim is None:
            return []
        oversample = k * 4
        sql = """
            SELECT c.*, v.distance
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.chunk_id
            WHERE v.embedding MATCH ? AND k = ?
        """
        args: list[object] = [_encode_vector(query_embedding), oversample]
        if source is not None:
            sql += " AND c.source = ?"
            args.append(source)
        sql += " ORDER BY v.distance LIMIT ?"
        args.append(k)
        rows = self._conn.execute(sql, args).fetchall()
        # sqlite-vec occasionally returns NULL distance for the last rows of
        # a small index (race between vec MATCH and the JOIN). Treat as
        # "max distance" so the row sorts to the bottom but doesn't crash.
        return [
            (_row_to_chunk(r), float(r["distance"]) if r["distance"] is not None else 1.0)
            for r in rows
        ]

    def search_fts(
        self, query: str, k: int, source: str | None = None
    ) -> list[tuple[MemoryChunk, float]]:
        if k <= 0 or not query.strip():
            return []
        sql = """
            SELECT c.*, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ?
        """
        args: list[object] = [query]
        if source is not None:
            sql += " AND c.source = ?"
            args.append(source)
        sql += " ORDER BY rank LIMIT ?"
        args.append(k)
        try:
            rows = self._conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as exc:
            # Malformed MATCH expression. Treat as no hits rather than 500
            # so a single bad query doesn't kill the whole RPC, but log so
            # operators can spot indexing/syntax bugs instead of getting a
            # silent empty result back.
            logger.warning(
                "FTS5 MATCH failed (treated as zero hits): query=%r source=%r err=%s",
                query,
                source,
                exc,
            )
            return []
        out: list[tuple[MemoryChunk, float]] = []
        for r in rows:
            rank = float(r["rank"])
            out.append((_row_to_chunk(r), bm25_rank_to_score(rank)))
        return out

    def chunk_file_mtimes(self) -> dict[str, float]:
        """``{relpath: mtime_seconds}`` for every indexed file."""
        rows = self._conn.execute("SELECT path, mtime FROM files").fetchall()
        return {r["path"]: float(r["mtime"]) for r in rows}

    def clear_all(self) -> None:
        """Wipe chunks + files + vec rows. Keep meta + embedding_cache."""
        self._conn.execute("DELETE FROM chunks_vec")
        self._conn.execute("DELETE FROM chunks_fts")
        self._conn.execute("DELETE FROM chunks")
        self._conn.execute("DELETE FROM files")
        self._conn.commit()

    # ── embedding cache ──

    def cache_get(self, provider: str, model: str, content_hash: str) -> list[float] | None:
        row = self._conn.execute(
            """
            SELECT embedding, dims FROM embedding_cache
            WHERE provider = ? AND model = ? AND content_hash = ?
            """,
            (provider, model, content_hash),
        ).fetchone()
        if row is None:
            return None
        dims = int(row["dims"])
        return list(struct.unpack(f"{dims}f", row["embedding"]))

    def cache_put(
        self,
        provider: str,
        model: str,
        content_hash: str,
        embedding: list[float],
    ) -> None:
        dims = len(embedding)
        self._conn.execute(
            """
            INSERT INTO embedding_cache (provider, model, content_hash, embedding,
                                         dims, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model, content_hash) DO UPDATE SET
              embedding=excluded.embedding,
              dims=excluded.dims,
              updated_at=excluded.updated_at
            """,
            (provider, model, content_hash, _encode_vector(embedding), dims, time.time()),
        )
        self._conn.commit()

    def cache_put_many(
        self,
        provider: str,
        model: str,
        items: list[tuple[str, list[float]]],
    ) -> None:
        """Bulk insert embedding cache entries with a single fsync.

        Items: (content_hash, embedding) pairs. ~N× faster than calling
        `cache_put` in a loop because fsync happens once per call instead of
        per row, and the prepared statement is reused.
        """
        if not items:
            return
        now = time.time()
        rows = [
            (
                provider,
                model,
                content_hash,
                _encode_vector(emb),
                len(emb),
                now,
            )
            for content_hash, emb in items
        ]
        self._conn.executemany(
            """
            INSERT INTO embedding_cache (provider, model, content_hash, embedding,
                                         dims, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, model, content_hash) DO UPDATE SET
              embedding=excluded.embedding,
              dims=excluded.dims,
              updated_at=excluded.updated_at
            """,
            rows,
        )
        self._conn.commit()

    def cache_size(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM embedding_cache").fetchone()
        return int(row["n"])

    # ── short_term (curated tier) ──

    def short_term_add(
        self,
        *,
        text: str,
        tags: list[str] | None = None,
        confidence: float = 0.5,
        source_chunk_id: str | None = None,
    ) -> str:
        """Promote / insert a fact into the curated tier. Returns the
        new short_term entry id (8 hex). Caller assigns no id."""
        import secrets
        import time as _time

        new_id = secrets.token_hex(4)
        joined_tags = ",".join(t.strip() for t in (tags or []) if t.strip())
        self._conn.execute(
            """
            INSERT INTO short_term
              (id, source_chunk_id, text, tags, confidence, promoted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id, source_chunk_id, text, joined_tags, float(confidence), _time.time()),
        )
        self._conn.commit()
        return new_id

    def short_term_list(
        self,
        *,
        tag: str | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        sql = "SELECT id, source_chunk_id, text, tags, confidence, promoted_at, last_reviewed_at, review_count, archived_at FROM short_term"
        clauses: list[str] = []
        args: list = []
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if tag:
            clauses.append("(',' || tags || ',') LIKE ?")
            args.append(f"%,{tag},%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY promoted_at DESC LIMIT ?"
        args.append(int(limit))
        rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "id": r["id"],
                "source_chunk_id": r["source_chunk_id"],
                "text": r["text"],
                "tags": [t for t in (r["tags"] or "").split(",") if t],
                "confidence": float(r["confidence"]),
                "promoted_at": float(r["promoted_at"]),
                "last_reviewed_at": float(r["last_reviewed_at"]) if r["last_reviewed_at"] else None,
                "review_count": int(r["review_count"]),
                "archived": r["archived_at"] is not None,
            }
            for r in rows
        ]

    def short_term_review(self, entry_id: str) -> bool:
        """Mark a curated entry as reviewed (bumps review_count +
        last_reviewed_at). Returns False if no such id."""
        import time as _time

        cur = self._conn.execute(
            "UPDATE short_term SET last_reviewed_at = ?, review_count = review_count + 1 WHERE id = ?",
            (_time.time(), entry_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def short_term_archive(self, entry_id: str) -> bool:
        """Soft-delete: hides the entry from default `list()` calls but
        keeps the row for audit. Returns False if no such id."""
        import time as _time

        cur = self._conn.execute(
            "UPDATE short_term SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (_time.time(), entry_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def short_term_count(self, *, include_archived: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM short_term"
        if not include_archived:
            sql += " WHERE archived_at IS NULL"
        return int(self._conn.execute(sql).fetchone()["n"])


def _row_to_chunk(row: sqlite3.Row) -> MemoryChunk:
    return MemoryChunk(
        id=row["id"],
        path=row["path"],
        source=row["source"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        text=row["text"],
        hash=row["hash"],
    )
