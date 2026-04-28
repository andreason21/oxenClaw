"""Active-memory blocking sub-agent."""

from __future__ import annotations

from pathlib import Path

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.active import (
    NO_RECALL_VALUES,
    ActiveMemoryConfig,
    ActiveMemoryRunner,
    format_active_memory_prelude,
)
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


def _model(provider: str = "ammod") -> Model:
    return Model(id="m", provider=provider, max_output_tokens=128, extra={"base_url": "x"})


def test_format_prelude_empty_returns_empty() -> None:
    assert format_active_memory_prelude("") == ""
    assert format_active_memory_prelude("  ") == ""


def test_format_prelude_renders_directive_block() -> None:
    out = format_active_memory_prelude("User lives in Suwon.")
    assert "Active memory recall" in out
    assert "Suwon" in out
    assert "Use it directly" in out


async def test_active_memory_returns_summary_text(tmp_path: Path) -> None:
    """Sub-agent returns a one-line summary; runner must surface it
    verbatim (after the NO_RECALL filter)."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="User lives in Suwon. ")
        yield TextDeltaEvent(delta="Relevant to the weather question.")
        yield StopEvent(reason="end_turn")

    register_provider_stream("ammod_ok", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        await retriever.save("User lives in Suwon, South Korea.")
        reg = InMemoryModelRegistry(models=[_model("ammod_ok")])
        runner = ActiveMemoryRunner(
            memory=retriever,
            main_model=reg.list()[0],
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"ammod_ok": "x"})),  # type: ignore[dict-item]
            config=ActiveMemoryConfig(enabled=True),
        )
        out = await runner.recall_for_turn(
            query="내가 사는 곳 날씨 알려줘",
            session_key="session-1",
        )
        assert "Suwon" in out
        assert "weather" in out

    finally:
        await retriever.aclose()


async def test_active_memory_filters_none_replies(tmp_path: Path) -> None:
    """Sub-agent emits 'none' → runner returns "" (no injection)."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="none")
        yield StopEvent(reason="end_turn")

    register_provider_stream("ammod_none", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        await retriever.save("totally unrelated fact about astronomy")
        reg = InMemoryModelRegistry(models=[_model("ammod_none")])
        runner = ActiveMemoryRunner(
            memory=retriever,
            main_model=reg.list()[0],
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"ammod_none": "x"})),  # type: ignore[dict-item]
            config=ActiveMemoryConfig(enabled=True),
        )
        out = await runner.recall_for_turn(query="anything", session_key="s")
        assert out == ""
    finally:
        await retriever.aclose()


async def test_active_memory_caches_within_ttl(tmp_path: Path) -> None:
    """Same (session, query) within TTL window must hit cache (one
    underlying call only)."""
    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield TextDeltaEvent(delta="cached fact")
        yield StopEvent(reason="end_turn")

    register_provider_stream("ammod_cache", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        await retriever.save("a fact about something.")
        reg = InMemoryModelRegistry(models=[_model("ammod_cache")])
        runner = ActiveMemoryRunner(
            memory=retriever,
            main_model=reg.list()[0],
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"ammod_cache": "x"})),  # type: ignore[dict-item]
            config=ActiveMemoryConfig(enabled=True, cache_ttl_seconds=60.0),
        )
        a = await runner.recall_for_turn(query="something", session_key="s")
        b = await runner.recall_for_turn(query="something", session_key="s")
        assert a == b
        assert state["calls"] == 1

    finally:
        await retriever.aclose()


async def test_active_memory_disabled_returns_empty(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path)
    try:
        reg = InMemoryModelRegistry(models=[_model("ammod_off")])
        runner = ActiveMemoryRunner(
            memory=retriever,
            main_model=reg.list()[0],
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"ammod_off": "x"})),  # type: ignore[dict-item]
            config=ActiveMemoryConfig(enabled=False),
        )
        out = await runner.recall_for_turn(query="x", session_key="s")
        assert out == ""
    finally:
        await retriever.aclose()


def test_no_recall_values_includes_common_decline_phrases() -> None:
    for v in ("none", "nothing useful", "no relevant memory", "n/a", "[]"):
        assert v in NO_RECALL_VALUES
