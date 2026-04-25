"""Gateway memory.* method dispatch using a stubbed retriever."""

from __future__ import annotations

from pathlib import Path

from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.gateway.memory_methods import register_memory_methods
from sampyclaw.gateway.router import Router
from sampyclaw.memory.retriever import MemoryRetriever
from tests._memory_stubs import StubEmbeddings


def _setup(tmp_path: Path) -> tuple[Router, MemoryRetriever]:
    paths = SampyclawPaths(home=tmp_path)
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
