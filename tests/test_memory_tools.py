"""Memory tool wiring — shape checks against a stubbed retriever."""

from __future__ import annotations

from pathlib import Path

from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.memory.retriever import MemoryRetriever
from sampyclaw.memory.tools import (
    memory_get_tool,
    memory_save_tool,
    memory_search_tool,
)
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


async def test_memory_save_tool_exposes_text_and_tags(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        assert tool.name == "memory_save"
        schema = tool.input_schema
        assert "text" in schema["properties"]
        assert "tags" in schema["properties"]
        out = await tool.execute({"text": "remember this", "tags": ["fact"]})
        assert "saved" in out
    finally:
        await r.aclose()


async def test_memory_search_tool_returns_citations(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("alpha bravo charlie")
        tool = memory_search_tool(r)
        out = await tool.execute({"query": "alpha", "k": 3})
        assert "inbox.md" in out
        assert ":" in out  # citation contains line range marker
    finally:
        await r.aclose()


async def test_memory_search_tool_no_match(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        tool = memory_search_tool(r)
        out = await tool.execute({"query": "anything"})
        assert "no memories" in out
    finally:
        await r.aclose()


async def test_memory_get_tool_reads_slice(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("recordable text")
        tool = memory_get_tool(r)
        out = await tool.execute({"path": "inbox.md", "from_line": 1, "lines": 50})
        assert "recordable text" in out
    finally:
        await r.aclose()


async def test_memory_get_tool_truncation_footer(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        long_text = "\n".join(f"line {i}" for i in range(1, 200))
        (r.memory_dir / "long.md").write_text(long_text)
        tool = memory_get_tool(r)
        out = await tool.execute({"path": "long.md", "from_line": 1, "lines": 50})
        assert "More available" in out
    finally:
        await r.aclose()
