"""Memory tool wiring — shape checks against a stubbed retriever."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.tools import (
    memory_get_tool,
    memory_save_tool,
    memory_search_tool,
)
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


async def test_tool_registry_resolves_namespaced_drift(tmp_path: Path) -> None:
    """Production hit: model called `memory:set_fact` (openclaw-style
    namespaced) — we register the tool as `memory_save`. The registry's
    canonicalise + semantic-alias table should fold it."""
    from oxenclaw.agents.tools import ToolRegistry

    r = _retriever(tmp_path)
    try:
        reg = ToolRegistry()
        reg.register(memory_save_tool(r))
        # Each of these is a real LLM emission seen in the wild OR a
        # plausible drift the alias table guards against.
        for variant in (
            "memory:set_fact",  # openclaw namespaced colon
            "memory.save",  # RPC dot notation
            "Memory_Save",  # case drift
            "memory-save",  # hyphen drift
            "remember",  # plain English
            "set_fact",  # bare verb
            "save_memory",  # noun-verb swap
            "memory_set",  # set/save confusion
        ):
            tool = reg.get(variant)
            assert tool is not None, f"registry should have folded {variant!r}"
            assert tool.name == "memory_save"
    finally:
        await r.aclose()


async def test_tool_registry_returns_none_for_truly_unknown(tmp_path: Path) -> None:
    """Sanity: bogus names that don't match any alias still return None
    so the run loop's 'tool X is not registered' path stays reachable."""
    from oxenclaw.agents.tools import ToolRegistry

    r = _retriever(tmp_path)
    try:
        reg = ToolRegistry()
        reg.register(memory_save_tool(r))
        assert reg.get("totally_made_up_tool") is None
        assert reg.get("xyzzy_42") is None
    finally:
        await r.aclose()


async def test_memory_save_accepts_content_alias(tmp_path: Path) -> None:
    """Real-world bug: GPT-4-class models sometimes emit
    `{content: ..., key: ...}` instead of `{text, tags}`. The schema
    used to reject those with `extra_forbidden`; the model-level
    `before` validator now folds aliases in."""
    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        out = await tool.execute({"content": "사용자는 수원에 거주한다.", "key": "거주지"})
        assert "saved" in out
        # Tag carried through.
        body = (r.memory_dir / "inbox.md").read_text(encoding="utf-8")
        assert "수원에 거주" in body
        assert "거주지" in body
    finally:
        await r.aclose()


async def test_memory_save_accepts_body_and_tag_aliases(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        out = await tool.execute({"body": "User prefers Korean replies.", "tag": "preference"})
        assert "saved" in out
    finally:
        await r.aclose()


async def test_memory_save_dedupes_tag_aliases(tmp_path: Path) -> None:
    """If both `tags` and a singular alias appear with overlap, the
    list de-duplicates and keeps insertion order."""
    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        await tool.execute(
            {
                "text": "x",
                "tags": ["fact"],
                "tag": "fact",  # dup → dropped
                "category": "user",  # new → appended
            }
        )
        body = (r.memory_dir / "inbox.md").read_text(encoding="utf-8")
        assert body.count("fact") == 1
        assert "user" in body
    finally:
        await r.aclose()


async def test_memory_save_drops_unknown_metadata_keys(tmp_path: Path) -> None:
    """Production hit: LLM emitted `{text, source: '사용자 발화'}` —
    `source` isn't a _SaveArgs field. Strict `extra=forbid` rejects
    the whole call. The before-validator strips unknown keys so
    sprinkled metadata (source / metadata / confidence / timestamp)
    no longer breaks the save."""
    r = _retriever(tmp_path)
    try:
        tool = memory_save_tool(r)
        # Real-world payload that crashed in production.
        out = await tool.execute(
            {
                "text": "사용자는 수원에 거주한다.",
                "source": "사용자 발화",
                "metadata": {"turn": 3},
                "confidence": 0.9,
            }
        )
        assert "saved" in out
        body = (r.memory_dir / "inbox.md").read_text(encoding="utf-8")
        assert "수원에 거주" in body
    finally:
        await r.aclose()


async def test_memory_search_drops_unknown_metadata_keys(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("alpha bravo charlie")
        tool = memory_search_tool(r)
        out = await tool.execute({"query": "alpha", "scope": "global", "agent_id": "x"})
        assert "inbox.md" in out
    finally:
        await r.aclose()


async def test_memory_search_accepts_question_alias(tmp_path: Path) -> None:
    r = _retriever(tmp_path)
    try:
        await r.save("alpha bravo charlie")
        tool = memory_search_tool(r)
        out = await tool.execute({"question": "alpha", "limit": 3})
        assert "inbox.md" in out
    finally:
        await r.aclose()


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
