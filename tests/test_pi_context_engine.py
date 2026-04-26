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


# ─── Phase 2: subagent / dispose / transcript-rewrite / cache types ──


async def test_legacy_prepare_subagent_spawn_returns_none() -> None:
    eng = LegacyContextEngine()
    result = await eng.prepare_subagent_spawn(
        parent_session_id="parent",
        child_session_key="parent:child:1",
    )
    assert result is None


async def test_legacy_on_subagent_ended_is_a_noop() -> None:
    eng = LegacyContextEngine()
    # Should accept all four end reasons without raising.
    for reason in ("deleted", "completed", "swept", "released"):
        await eng.on_subagent_ended(child_session_key="c1", reason=reason)


async def test_legacy_dispose_is_a_noop() -> None:
    eng = LegacyContextEngine()
    await eng.dispose()


async def test_compact_accepts_compaction_target_budget() -> None:
    """Phase 2 added `compaction_target` to compact. Legacy should
    accept both values and route through `delegate_compaction_to_runtime`
    with the appropriate threshold."""
    from oxenclaw.pi.messages import UserMessage

    eng = LegacyContextEngine()
    msgs = [UserMessage(content="hi")]
    res = await eng.compact(
        session_id="s1",
        messages=msgs,
        token_budget=128_000,
        compaction_target="budget",
    )
    assert res.ok is True


def test_clear_for_owner_removes_only_that_owners_slots() -> None:
    from oxenclaw.pi.context_engine.registry import (
        clear_context_engines_for_owner,
        list_slots,
        register_context_engine_for_owner,
    )

    register_context_engine_for_owner(
        slot="active-memory", owner="plugin-a", factory=LegacyContextEngine
    )
    register_context_engine_for_owner(
        slot="memory-wiki", owner="plugin-b", factory=LegacyContextEngine
    )
    cleared = clear_context_engines_for_owner("plugin-a")
    assert cleared == ["active-memory"]
    assert "memory-wiki" in list_slots()
    assert "active-memory" not in list_slots()


def test_get_factory_returns_factory_without_instantiating() -> None:
    from oxenclaw.pi.context_engine.registry import (
        get_context_engine_factory,
        register_context_engine_for_owner,
    )

    counter = {"calls": 0}

    def factory():
        counter["calls"] += 1
        return LegacyContextEngine()

    register_context_engine_for_owner(slot="custom", owner="o1", factory=factory)
    f = get_context_engine_factory("custom")
    assert f is factory
    assert counter["calls"] == 0  # not instantiated yet


def test_transcript_rewrite_types_construct() -> None:
    from oxenclaw.pi.context_engine import (
        TranscriptRewriteReplacement,
        TranscriptRewriteRequest,
        TranscriptRewriteResult,
    )
    from oxenclaw.pi.messages import UserMessage

    req = TranscriptRewriteRequest(
        replacements=[TranscriptRewriteReplacement(entry_id="e1", message=UserMessage(content="x"))]
    )
    assert len(req.replacements) == 1
    res = TranscriptRewriteResult(changed=True, bytes_freed=42, rewritten_entries=1)
    assert res.bytes_freed == 42


def test_prompt_cache_observation_types_construct() -> None:
    from oxenclaw.pi.context_engine import (
        PromptCacheInfo,
        PromptCacheObservation,
        PromptCacheObservationChange,
        PromptCacheUsage,
    )

    info = PromptCacheInfo(
        retention="long",
        usage=PromptCacheUsage(
            cache_read_input_tokens=512,
            cache_creation_input_tokens=128,
            input_tokens=64,
            output_tokens=32,
        ),
        observation=PromptCacheObservation(
            changes=[PromptCacheObservationChange(code="hit", detail="warm")]
        ),
    )
    assert info.retention == "long"
    assert info.usage.cache_read_input_tokens == 512
    assert info.observation.changes[0].code == "hit"


def test_runtime_context_typed_fields_default_none() -> None:
    from oxenclaw.pi.context_engine import ContextEngineRuntimeContext

    ctx = ContextEngineRuntimeContext()
    assert ctx.allow_deferred_compaction_execution is False
    assert ctx.token_budget is None
    assert ctx.current_token_count is None
    assert ctx.prompt_cache is None
    assert ctx.rewrite_transcript_entries is None
    assert ctx.extra == {}


def test_maintenance_result_aliases_transcript_rewrite_result() -> None:
    """Per openclaw types.ts:
    `ContextEngineMaintenanceResult = TranscriptRewriteResult`."""
    from oxenclaw.pi.context_engine import (
        ContextEngineMaintenanceResult,
        TranscriptRewriteResult,
    )

    assert ContextEngineMaintenanceResult is TranscriptRewriteResult
