"""Pluggable context-management subsystem.

Mirrors openclaw's `src/context-engine/`. A `ContextEngine` decides
*how the model context window is filled* before each model call. The
default `LegacyContextEngine` just passes the recent messages through
and delegates compaction to `oxenclaw.pi.compaction.maybe_compact` —
identical behaviour to pre-rc.16 PiAgent.

Third parties register a custom engine via:

    from oxenclaw.pi.context_engine import register_context_engine
    register_context_engine(owner="my-active-memory-plugin", factory=lambda: MyEngine())

Then the gateway resolves it by slot name (configured in `config.yaml`)
or falls back to legacy. The seven lifecycle hooks
(`bootstrap`/`maintain`/`ingest`/`ingest_batch`/`after_turn`/`assemble`/
`compact`) match openclaw's `ContextEngine` interface 1:1 so that
porting an active-memory or memory-wiki style plugin from openclaw is
mechanical translation.
"""

from oxenclaw.pi.context_engine.delegate import delegate_compaction_to_runtime
from oxenclaw.pi.context_engine.legacy import LegacyContextEngine, register_legacy_context_engine
from oxenclaw.pi.context_engine.openclaw_engine import OpenclawContextEngine
from oxenclaw.pi.context_engine.registry import (
    ContextEngineFactory,
    ContextEngineRegistrationResult,
    clear_context_engines_for_owner,
    ensure_context_engines_initialized,
    get_context_engine_factory,
    register_context_engine,
    resolve_context_engine,
)
from oxenclaw.pi.context_engine.types import (
    AssembleResult,
    BootstrapResult,
    CompactionTarget,
    CompactResult,
    ContextEngine,
    ContextEngineInfo,
    ContextEngineMaintenanceResult,
    ContextEngineRuntimeContext,
    IngestBatchResult,
    IngestResult,
    PromptCacheInfo,
    PromptCacheObservation,
    PromptCacheObservationChange,
    PromptCacheObservationChangeCode,
    PromptCacheRetention,
    PromptCacheUsage,
    SubagentEndReason,
    SubagentSpawnPreparation,
    TranscriptRewriteReplacement,
    TranscriptRewriteRequest,
    TranscriptRewriteResult,
)

__all__ = [
    "AssembleResult",
    "BootstrapResult",
    "CompactResult",
    "CompactionTarget",
    "ContextEngine",
    "ContextEngineFactory",
    "ContextEngineInfo",
    "ContextEngineMaintenanceResult",
    "ContextEngineRegistrationResult",
    "ContextEngineRuntimeContext",
    "IngestBatchResult",
    "IngestResult",
    "LegacyContextEngine",
    "OpenclawContextEngine",
    "PromptCacheInfo",
    "PromptCacheObservation",
    "PromptCacheObservationChange",
    "PromptCacheObservationChangeCode",
    "PromptCacheRetention",
    "PromptCacheUsage",
    "SubagentEndReason",
    "SubagentSpawnPreparation",
    "TranscriptRewriteReplacement",
    "TranscriptRewriteRequest",
    "TranscriptRewriteResult",
    "clear_context_engines_for_owner",
    "delegate_compaction_to_runtime",
    "ensure_context_engines_initialized",
    "get_context_engine_factory",
    "register_context_engine",
    "register_legacy_context_engine",
    "resolve_context_engine",
]
