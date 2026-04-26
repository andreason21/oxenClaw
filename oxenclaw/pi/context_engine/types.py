"""ContextEngine Protocol + result types.

1:1 port of openclaw `src/context-engine/types.ts`. Names are
snake_case'd; structure preserved so an engine ported from TypeScript
maps field-for-field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from oxenclaw.pi.messages import AgentMessage

# ─── Compact, subagent, transcript-rewrite, prompt-cache types ───────

CompactionTarget = Literal["budget", "threshold"]
SubagentEndReason = Literal["deleted", "completed", "swept", "released"]
PromptCacheRetention = Literal["none", "short", "long", "in_memory", "24h"]
PromptCacheObservationChangeCode = Literal[
    "hit",
    "miss",
    "create",
    "ttl_extended",
    "ttl_decayed",
    "evicted",
]


@dataclass
class ContextEngineInfo:
    """Engine identity + metadata."""

    id: str
    name: str
    version: str | None = None
    # When True the engine handles its own compaction lifecycle.
    owns_compaction: bool = False
    # "foreground" runs maintenance inline; "background" defers to a task.
    turn_maintenance_mode: str = "foreground"


@dataclass
class AssembleResult:
    # Ordered messages to use as model context.
    messages: list[AgentMessage]
    # Estimated total tokens in assembled context.
    estimated_tokens: int
    # Optional engine-provided instructions prepended to the runtime system prompt.
    system_prompt_addition: str | None = None


@dataclass
class CompactResult:
    ok: bool
    compacted: bool
    reason: str | None = None
    # When `compacted=True`, the post-compaction details.
    summary: str | None = None
    first_kept_entry_id: str | None = None
    tokens_before: int = 0
    tokens_after: int | None = None


@dataclass
class IngestResult:
    # False if duplicate or no-op.
    ingested: bool


@dataclass
class IngestBatchResult:
    ingested_count: int


@dataclass
class BootstrapResult:
    bootstrapped: bool
    imported_messages: int | None = None
    reason: str | None = None


@dataclass
class TranscriptRewriteReplacement:
    """One entry replacement in a branch-and-reappend rewrite."""

    entry_id: str
    message: AgentMessage


@dataclass
class TranscriptRewriteRequest:
    replacements: list[TranscriptRewriteReplacement]


@dataclass
class TranscriptRewriteResult:
    """Result of a transcript rewrite — also used as `ContextEngineMaintenanceResult`."""

    changed: bool
    bytes_freed: int = 0
    rewritten_entries: int = 0
    reason: str | None = None


# Maintenance returns the same shape as a transcript rewrite —
# the maintain hook may rewrite or no-op.
ContextEngineMaintenanceResult = TranscriptRewriteResult


@dataclass
class SubagentSpawnPreparation:
    """What an engine returns from `prepare_subagent_spawn` so the host
    can roll back pre-spawn setup if launch fails."""

    rollback: Any  # Callable[[], None | Awaitable[None]]


@dataclass
class PromptCacheUsage:
    """Per-turn prompt-cache token counters."""

    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class PromptCacheObservationChange:
    """One state-change event observed during a turn."""

    code: PromptCacheObservationChangeCode
    detail: str | None = None


@dataclass
class PromptCacheObservation:
    """All cache-state changes observed during a turn."""

    changes: list[PromptCacheObservationChange] = field(default_factory=list)


@dataclass
class PromptCacheInfo:
    """Aggregated prompt-cache info passed via `runtime_context.prompt_cache`."""

    retention: PromptCacheRetention = "none"
    usage: PromptCacheUsage = field(default_factory=PromptCacheUsage)
    observation: PromptCacheObservation = field(default_factory=PromptCacheObservation)


@dataclass
class ContextEngineRuntimeContext:
    """Caller-owned context the engine may rely on.

    Mirrors openclaw's optional runtimeContext bag with the same five
    typed fields plus an `extra` record for host-specific extensions.
    The host fills in whatever it can supply, the engine reads what it
    needs and ignores the rest.
    """

    # Set by the host when this maintenance run is allowed to consume
    # deferred compaction debt accumulated across prior turns.
    allow_deferred_compaction_execution: bool = False
    # Runtime-resolved context window budget for the active model call.
    token_budget: int | None = None
    # Best-effort current prompt/context token estimate for this turn.
    current_token_count: int | None = None
    # Optional prompt-cache telemetry for cache-aware engines.
    prompt_cache: PromptCacheInfo | None = None
    # Safe transcript rewrite helper implemented by the runtime.
    # Engines decide what to rewrite; the runtime owns how the session
    # DAG is updated on disk.
    rewrite_transcript_entries: Any = (
        None  # async (req: TranscriptRewriteRequest) -> TranscriptRewriteResult
    )
    # Bag for host-specific extensions that don't warrant a typed
    # field (e.g. PiAgent passes the live `AgentSession` here so the
    # legacy engine's `compact()` can mutate it via the existing
    # `maybe_compact` path).
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ContextEngine(Protocol):
    """Plugin contract for context window assembly + compaction.

    All hooks except `ingest` and `assemble` are optional; the legacy
    engine provides defaults. Engines are resolved by `slot` name (set
    in config) and fall back to the legacy implementation when no
    plugin claims the slot.
    """

    info: ContextEngineInfo

    async def bootstrap(
        self,
        *,
        session_id: str,
        session_file: str | None = None,
        session_key: str | None = None,
    ) -> BootstrapResult:
        """Initialize per-session state. Optional historical import."""
        ...

    async def maintain(
        self,
        *,
        session_id: str,
        session_file: str | None = None,
        session_key: str | None = None,
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> ContextEngineMaintenanceResult:
        """Run transcript maintenance after bootstrap / turn / compaction."""
        ...

    async def ingest(
        self,
        *,
        session_id: str,
        message: AgentMessage,
        session_key: str | None = None,
        is_heartbeat: bool = False,
    ) -> IngestResult:
        """Ingest one message into the engine's store."""
        ...

    async def ingest_batch(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        session_key: str | None = None,
        is_heartbeat: bool = False,
    ) -> IngestBatchResult:
        """Ingest a completed turn batch as a single unit."""
        ...

    async def after_turn(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        pre_prompt_message_count: int,
        session_file: str | None = None,
        session_key: str | None = None,
        auto_compaction_summary: str | None = None,
        is_heartbeat: bool = False,
        token_budget: int | None = None,
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> None:
        """Post-turn lifecycle work after a run attempt completes."""
        ...

    async def assemble(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        token_budget: int | None = None,
        session_key: str | None = None,
        available_tools: set[str] | None = None,
    ) -> AssembleResult:
        """Build the context to feed the model under a token budget."""
        ...

    async def compact(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        token_budget: int | None = None,
        current_token_count: int | None = None,
        session_file: str | None = None,
        force: bool = False,
        compaction_target: CompactionTarget = "threshold",
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> CompactResult:
        """Compact the transcript when it grows past the budget.

        `compaction_target` controls what the engine targets:
        - `"threshold"` — compact down to the high-water mark before
          the next call hits the wall (default; matches openclaw).
        - `"budget"` — compact aggressively to the smallest size that
          still preserves required tail context. Used by manual
          `/compact` invocations.
        """
        ...

    async def prepare_subagent_spawn(
        self,
        *,
        parent_session_id: str,
        child_session_key: str,
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> SubagentSpawnPreparation | None:
        """Optional pre-spawn hook returning a rollback callback.

        Engines that mutate parent-session state when a subagent
        spawns (e.g. mark a transcript entry as "delegated") return
        the rollback so the host can undo if the subagent launch
        actually fails."""
        ...

    async def on_subagent_ended(
        self,
        *,
        child_session_key: str,
        reason: SubagentEndReason,
    ) -> None:
        """Optional notification when a subagent terminates."""
        ...

    async def dispose(self) -> None:
        """Optional cleanup hook called when the engine is being unloaded.

        Engines that hold long-lived resources (DB connections, threads,
        file handles) free them here. Always called from the host on
        shutdown / plugin unload.
        """
        ...


__all__ = [
    "AssembleResult",
    "BootstrapResult",
    "CompactResult",
    "CompactionTarget",
    "ContextEngine",
    "ContextEngineInfo",
    "ContextEngineMaintenanceResult",
    "ContextEngineRuntimeContext",
    "IngestBatchResult",
    "IngestResult",
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
]
