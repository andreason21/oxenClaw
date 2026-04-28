"""Gateway memory.* method dispatch using a stubbed retriever."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.memory_methods import register_memory_methods
from oxenclaw.gateway.router import Router
from oxenclaw.memory.retriever import MemoryRetriever
from tests._memory_stubs import StubEmbeddings


def _setup(tmp_path: Path) -> tuple[Router, MemoryRetriever]:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    router = Router()
    register_memory_methods(router, retriever)
    return router, retriever


async def _call(router: Router, method: str, params: dict) -> dict:
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert resp.error is None, resp.error
    return resp.result  # type: ignore[return-value]


async def test_memory_save_then_search_dispatch(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        save_res = await _call(router, "memory.save", {"text": "remember xyz123"})
        assert save_res["ok"] is True
        assert "report" in save_res

        search_res = await _call(router, "memory.search", {"query": "xyz123", "k": 3})
        assert search_res["ok"] is True
        assert search_res["hits"]
        first = search_res["hits"][0]
        assert "chunk" in first
        assert "citation" in first
        assert first["chunk"]["path"] == "inbox.md"
    finally:
        await retriever.aclose()


async def test_memory_stats_dispatch(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(router, "memory.stats", {})
        assert res["ok"] is True
        assert "total_files" in res
        assert "total_chunks" in res
        assert "path" in res
        assert "meta" in res
    finally:
        await retriever.aclose()


async def test_memory_list_dispatch(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        await _call(router, "memory.save", {"text": "x"})
        res = await _call(router, "memory.list", {})
        assert res["ok"] is True
        assert any(f["path"] == "inbox.md" for f in res["files"])
    finally:
        await retriever.aclose()


async def test_memory_get_dispatch(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        await _call(router, "memory.save", {"text": "first line"})
        res = await _call(router, "memory.get", {"path": "inbox.md", "from_line": 1, "lines": 100})
        assert res["ok"] is True
        assert "first line" in res["read"]["text"]
    finally:
        await retriever.aclose()


async def test_memory_get_traversal_rejected(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(
            router, "memory.get", {"path": "../etc/passwd", "from_line": 1, "lines": 1}
        )
        assert res["ok"] is False
        assert "escapes" in res["error"]
    finally:
        await retriever.aclose()


async def test_memory_sync_dispatch(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(router, "memory.sync", {})
        assert res["ok"] is True
        assert "report" in res
    finally:
        await retriever.aclose()


async def test_memory_delete_by_path_clears_chunks(tmp_path: Path) -> None:
    """memory.delete with path arg removes the file row + every chunk
    keyed off that path across vec / fts / chunks tables."""
    router, retriever = _setup(tmp_path)
    try:
        await _call(router, "memory.save", {"text": "to be deleted"})
        before = await _call(router, "memory.list", {})
        assert any(f["path"] == "inbox.md" for f in before["files"])
        res = await _call(router, "memory.delete", {"path": "inbox.md"})
        assert res["ok"] is True
        assert res["deleted_path"] == "inbox.md"
        after = await _call(router, "memory.list", {})
        assert all(f["path"] != "inbox.md" for f in after["files"])
    finally:
        await retriever.aclose()


async def test_memory_delete_requires_chunk_id_or_path(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(router, "memory.delete", {})
        assert res["ok"] is False
        assert "chunk_id" in res["error"] or "path" in res["error"]
    finally:
        await retriever.aclose()


async def test_memory_export_then_import_round_trip(tmp_path: Path) -> None:
    """Export returns a JSON envelope; import on a fresh store
    reinstates the file + chunk rows so memory.list sees them. Vectors
    are intentionally not in the JSON — caller is expected to run
    memory.sync afterwards to regenerate."""
    router_a, retriever_a = _setup(tmp_path / "a")
    router_b, retriever_b = _setup(tmp_path / "b")
    try:
        await _call(router_a, "memory.save", {"text": "alpha bravo charlie"})
        export = await _call(router_a, "memory.export", {})
        assert export["ok"] is True
        assert export["schema_version"] == 1
        assert export["files"], "export should carry at least one file"
        assert export["chunks"], "export should carry at least one chunk"

        # Import into a fresh store.
        imp = await _call(
            router_b,
            "memory.import",
            {
                "files": export["files"],
                "chunks": export["chunks"],
                "overwrite": True,
            },
        )
        assert imp["ok"] is True
        assert imp["imported_files"] >= 1
        assert imp["imported_chunks"] >= 1

        listing = await _call(router_b, "memory.list", {})
        assert any(f["path"] == "inbox.md" for f in listing["files"])
    finally:
        await retriever_a.aclose()
        await retriever_b.aclose()


async def test_memory_promote_text_creates_curated_entry(tmp_path: Path) -> None:
    """memory.promote with raw text creates a short_term entry the
    list endpoint can find."""
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(
            router,
            "memory.promote",
            {
                "text": "Project deadline is May 1.",
                "tags": ["deadline", "project"],
                "confidence": 0.9,
            },
        )
        assert res["ok"] is True
        assert res["id"]
        assert res["promoted_from"] is None
        listing = await _call(router, "memory.short_term_list", {})
        assert listing["count"] == 1
        entry = listing["entries"][0]
        assert entry["text"] == "Project deadline is May 1."
        assert set(entry["tags"]) == {"deadline", "project"}
        assert entry["confidence"] == 0.9
    finally:
        await retriever.aclose()


async def test_memory_promote_chunk_id_round_trip(tmp_path: Path) -> None:
    """Promoting an existing chunk records source_chunk_id linkage."""
    router, retriever = _setup(tmp_path)
    try:
        await _call(router, "memory.save", {"text": "alpha beta gamma"})
        search = await _call(router, "memory.search", {"query": "alpha", "k": 1})
        chunk_id = search["hits"][0]["chunk"]["id"]
        promoted = await _call(
            router,
            "memory.promote",
            {"chunk_id": chunk_id, "tags": ["fact"]},
        )
        assert promoted["ok"] is True
        assert promoted["promoted_from"] == chunk_id
        rows = retriever.store.short_term_list()
        assert rows[0]["source_chunk_id"] == chunk_id
    finally:
        await retriever.aclose()


async def test_memory_short_term_review_and_archive(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        promoted = await _call(router, "memory.promote", {"text": "fact"})
        eid = promoted["id"]
        # Review bumps review_count.
        rev = await _call(router, "memory.short_term_review", {"id": eid})
        assert rev["ok"] is True
        rows = retriever.store.short_term_list()
        assert rows[0]["review_count"] == 1
        assert rows[0]["last_reviewed_at"] is not None
        # Archive hides it from default list.
        arc = await _call(router, "memory.short_term_archive", {"id": eid})
        assert arc["ok"] is True
        active = await _call(router, "memory.short_term_list", {})
        assert active["count"] == 0
        all_rows = await _call(router, "memory.short_term_list", {"include_archived": True})
        assert all_rows["count"] == 1
        assert all_rows["entries"][0]["archived"] is True
    finally:
        await retriever.aclose()


async def test_memory_promote_rejects_missing_arg(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        res = await _call(router, "memory.promote", {})
        assert res["ok"] is False
        assert "chunk_id" in res["error"] or "text" in res["error"]
    finally:
        await retriever.aclose()


async def test_memory_export_filters_by_source(tmp_path: Path) -> None:
    router, retriever = _setup(tmp_path)
    try:
        await _call(router, "memory.save", {"text": "source-test"})
        # The inbox source is "memory" by default.
        res = await _call(router, "memory.export", {"source": "memory"})
        assert res["ok"] is True
        assert all(c["source"] == "memory" for c in res["chunks"])
        # Filtering to a non-existent source returns an empty payload, not an error.
        res = await _call(router, "memory.export", {"source": "nope"})
        assert res["ok"] is True
        assert res["files"] == []
        assert res["chunks"] == []
    finally:
        await retriever.aclose()
