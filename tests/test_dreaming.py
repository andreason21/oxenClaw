"""Dreaming pipeline — post-session narrative consolidation."""

from __future__ import annotations

from pathlib import Path

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.dreaming import DreamingConfig, dream_session
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent
from tests._memory_stubs import StubEmbeddings


def _setup(tmp_path: Path) -> tuple[ConversationHistory, MemoryRetriever, Model]:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    hist_path = paths.session_file("a", "s1")
    hist = ConversationHistory(hist_path)
    for i in range(6):
        hist.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"line {i}"})
    hist.save()
    model = Model(id="m", provider="dreammod", max_output_tokens=512, extra={"base_url": "x"})
    return hist, retriever, model


async def test_dream_disabled_returns_skipped(tmp_path: Path) -> None:
    hist, retriever, model = _setup(tmp_path)
    try:
        result = await dream_session(
            agent_id="a",
            session_key="s1",
            history=hist,
            memory=retriever,
            sub_model=model,
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"dreammod": "x"})),  # type: ignore[dict-item]
            config=DreamingConfig(enabled=False),
        )
        assert result.skipped_reason == "dreaming disabled"
        assert result.facts_extracted == 0
    finally:
        await retriever.aclose()


async def test_dream_short_session_skipped(tmp_path: Path) -> None:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    hist = ConversationHistory(paths.session_file("a", "s1"))
    hist.append({"role": "user", "content": "hi"})
    hist.save()
    model = Model(id="m", provider="dreammod_short", max_output_tokens=128, extra={"base_url": "x"})
    try:
        result = await dream_session(
            agent_id="a",
            session_key="s1",
            history=hist,
            memory=retriever,
            sub_model=model,
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"dreammod_short": "x"})),  # type: ignore[dict-item]
            config=DreamingConfig(enabled=True, min_session_turns=4),
        )
        assert "min" in (result.skipped_reason or "")
    finally:
        await retriever.aclose()


async def test_dream_extracts_facts_and_promotes_high_confidence(tmp_path: Path) -> None:
    """Sub-agent returns 2 facts (one high-confidence, one low). The
    runner saves both to inbox and promotes ONLY the high-confidence
    one to short_term."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta='{"facts": [')
        yield TextDeltaEvent(
            delta='{"text": "User lives in Suwon.", "confidence": 0.9, "tags": ["location"]},'
        )
        yield TextDeltaEvent(
            delta='{"text": "User mentioned coffee briefly.", "confidence": 0.3, "tags": ["chatter"]}'
        )
        yield TextDeltaEvent(delta="]}")
        yield StopEvent(reason="end_turn")

    register_provider_stream("dreammod_ok", fake_stream)
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    hist = ConversationHistory(paths.session_file("a", "s1"))
    for i in range(8):
        hist.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"})
    hist.save()
    model = Model(id="m", provider="dreammod_ok", max_output_tokens=512, extra={"base_url": "x"})
    try:
        result = await dream_session(
            agent_id="a",
            session_key="s1",
            history=hist,
            memory=retriever,
            sub_model=model,
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"dreammod_ok": "x"})),  # type: ignore[dict-item]
            config=DreamingConfig(enabled=True, auto_promote_threshold=0.7),
        )
        assert result.error is None
        assert result.facts_extracted == 2
        assert result.facts_promoted == 1  # only the 0.9-confidence one
        # Inbox carries both facts.
        inbox = (paths.home / "memory" / "inbox.md").read_text()
        assert "Suwon" in inbox
        assert "coffee" in inbox
    finally:
        await retriever.aclose()


async def test_dream_handles_garbage_model_output(tmp_path: Path) -> None:
    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="this is not json at all")
        yield StopEvent(reason="end_turn")

    register_provider_stream("dreammod_bad", fake_stream)
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    retriever = MemoryRetriever.for_root(paths, StubEmbeddings())
    hist = ConversationHistory(paths.session_file("a", "s1"))
    for i in range(6):
        hist.append({"role": "user", "content": f"line {i}"})
    hist.save()
    model = Model(id="m", provider="dreammod_bad", max_output_tokens=128, extra={"base_url": "x"})
    try:
        result = await dream_session(
            agent_id="a",
            session_key="s1",
            history=hist,
            memory=retriever,
            sub_model=model,
            api_resolver=lambda m: resolve_api(m, InMemoryAuthStorage({"dreammod_bad": "x"})),  # type: ignore[dict-item]
            config=DreamingConfig(enabled=True),
        )
        # No facts → skipped, NOT errored.
        assert result.facts_extracted == 0
        assert result.skipped_reason == "no durable facts extracted"
    finally:
        await retriever.aclose()
