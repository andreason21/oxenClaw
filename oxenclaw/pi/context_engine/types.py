"""ContextEngine Protocol + result types.

1:1 port of openclaw `src/context-engine/types.ts`. Names are
snake_case'd; structure preserved so an engine ported from TypeScript
maps field-for-field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from oxenclaw.pi.messages import AgentMessage


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
class ContextEngineMaintenanceResult:
    ok: bool
    reason: str | None = None


@dataclass
class ContextEngineRuntimeContext:
    """Caller-owned context the engine may rely on.

    Mirrors openclaw's optional runtimeContext bag — the host fills in
    whatever it can supply, the engine reads what it needs and ignores
    the rest. Concrete fields stay loose by design.
    """

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
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> CompactResult:
        """Compact the transcript when it grows past the budget."""
        ...


__all__ = [
    "AssembleResult",
    "BootstrapResult",
    "CompactResult",
    "ContextEngine",
    "ContextEngineInfo",
    "ContextEngineMaintenanceResult",
    "ContextEngineRuntimeContext",
    "IngestBatchResult",
    "IngestResult",
]
