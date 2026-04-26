"""Tests for the pluggable ContextEngine subsystem.

Mirrors the contract laid out in openclaw `src/context-engine/types.ts`
and the registry semantics from `src/context-engine/registry.ts`.
"""

from __future__ import annotations

import pytest

from oxenclaw.pi.context_engine import (
    AssembleResult,
    BootstrapResult,
    CompactResult,
    ContextEngineInfo,
    IngestBatchResult,
    IngestResult,
    LegacyContextEngine,
    ensure_context_engines_initialized,
    register_context_engine,
    resolve_context_engine,
)
from oxenclaw.pi.context_engine.registry import (
    _reset_for_tests,
    list_slots,
    register_context_engine_for_owner,
)
from oxenclaw.pi.messages import SystemMessage, TextContent, UserMessage


@pytest.fixture(autouse=True)
def _isolated_registry():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_legacy_engine_advertises_identity() -> None:
    eng = LegacyContextEngine()
    assert isinstance(eng.info, ContextEngineInfo)
    assert eng.info.id == "legacy"
    assert eng.info.owns_compaction is False


def test_ensure_initialized_registers_legacy_under_legacy_slot() -> None:
    ensure_context_engines_initialized()
    assert "legacy" in list_slots()


def test_ensure_initialized_is_idempotent() -> None:
    ensure_context_engines_initialized()
    ensure_context_engines_initialized()
    assert list_slots() == ["legacy"]


async def test_resolve_returns_legacy_engine_after_init() -> None:
    ensure_context_engines_initialized()
    eng = await resolve_context_engine("legacy")
    assert isinstance(eng, LegacyContextEngine)


async def test_resolve_unknown_slot_returns_none() -> None:
    ensure_context_engines_initialized()
    assert await resolve_context_engine("not-a-slot") is None


def test_register_refuses_cross_owner_collision() -> None:
    register_context_engine_for_owner(
        slot="memory-wiki",
        owner="plugin-a",
        factory=LegacyContextEngine,
    )
    second = register_context_engine_for_owner(
        slot="memory-wiki",
        owner="plugin-b",
        factory=LegacyContextEngine,
    )
    assert second.ok is False
    assert second.existing_owner == "plugin-a"


def test_register_allows_same_owner_refresh() -> None:
    register_context_engine_for_owner(
        slot="memory-wiki",
        owner="plugin-a",
        factory=LegacyContextEngine,
    )
    second = register_context_engine_for_owner(
        slot="memory-wiki",
        owner="plugin-a",
        factory=LegacyContextEngine,
    )
    assert second.ok is True


async def test_legacy_assemble_passes_messages_through() -> None:
    eng = LegacyContextEngine()
    msgs = [
        SystemMessage(content="be brief"),
        UserMessage(content=[TextContent(text="hi")]),
    ]
    out = await eng.assemble(session_id="s1", messages=msgs)
    assert isinstance(out, AssembleResult)
    assert out.messages == msgs
    assert out.estimated_tokens >= 0


async def test_legacy_ingest_is_a_noop() -> None:
    eng = LegacyContextEngine()
    res = await eng.ingest(session_id="s1", message=UserMessage(content="hello"))
    assert isinstance(res, IngestResult)
    assert res.ingested is False


async def test_legacy_ingest_batch_is_a_noop() -> None:
    eng = LegacyContextEngine()
    res = await eng.ingest_batch(
        session_id="s1",
        messages=[UserMessage(content="a"), UserMessage(content="b")],
    )
    assert isinstance(res, IngestBatchResult)
    assert res.ingested_count == 0


async def test_legacy_bootstrap_reports_zero_imports() -> None:
    eng = LegacyContextEngine()
    res = await eng.bootstrap(session_id="s1")
    assert isinstance(res, BootstrapResult)
    assert res.bootstrapped is True
    assert res.imported_messages == 0


async def test_legacy_compact_short_history_below_threshold() -> None:
    eng = LegacyContextEngine()
    msgs = [UserMessage(content="hello world")]
    res = await eng.compact(
        session_id="s1",
        messages=msgs,
        token_budget=128_000,
    )
    assert isinstance(res, CompactResult)
    # A single short message can't possibly cross 85% of 128K.
    assert res.ok is True
    assert res.compacted is False


def test_register_helper_uses_host_owner_default() -> None:
    res = register_context_engine(slot="custom", factory=LegacyContextEngine)
    assert res.ok is True
    assert "custom" in list_slots()
