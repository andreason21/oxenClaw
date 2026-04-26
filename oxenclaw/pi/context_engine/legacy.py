"""Legacy ContextEngine — pass-through fallback identical to pre-rc.16 behavior.

When no plugin claims a slot, this engine fields the calls so PiAgent
keeps working without any context-engine integration. Behaviour mirrors
what PiAgent did inline before rc.16 introduced the protocol:

- ingest / ingest_batch / after_turn — no-ops (PiAgent already owns the
  Session via SessionManager).
- assemble — return messages as-is, with a rough token estimate from
  `oxenclaw.pi.tokens.estimate_tokens`.
- compact — delegate to the runtime compaction in
  `oxenclaw.pi.compaction.maybe_compact`.
"""

from __future__ import annotations

from oxenclaw.pi.context_engine.types import (
    AssembleResult,
    BootstrapResult,
    CompactResult,
    ContextEngineInfo,
    ContextEngineMaintenanceResult,
    ContextEngineRuntimeContext,
    IngestBatchResult,
    IngestResult,
)
from oxenclaw.pi.messages import AgentMessage
from oxenclaw.pi.tokens import estimate_tokens


class LegacyContextEngine:
    """Stateless fallback engine. Multiple sessions share one instance."""

    info = ContextEngineInfo(
        id="legacy",
        name="Legacy pass-through",
        version="1.0",
        owns_compaction=False,
        turn_maintenance_mode="foreground",
    )

    async def bootstrap(
        self,
        *,
        session_id: str,
        session_file: str | None = None,
        session_key: str | None = None,
    ) -> BootstrapResult:
        return BootstrapResult(bootstrapped=True, imported_messages=0)

    async def maintain(
        self,
        *,
        session_id: str,
        session_file: str | None = None,
        session_key: str | None = None,
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> ContextEngineMaintenanceResult:
        return ContextEngineMaintenanceResult(ok=True)

    async def ingest(
        self,
        *,
        session_id: str,
        message: AgentMessage,
        session_key: str | None = None,
        is_heartbeat: bool = False,
    ) -> IngestResult:
        # PiAgent's SessionManager already owns persistence; the engine
        # has nothing to add for the pass-through case.
        return IngestResult(ingested=False)

    async def ingest_batch(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        session_key: str | None = None,
        is_heartbeat: bool = False,
    ) -> IngestBatchResult:
        return IngestBatchResult(ingested_count=0)

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
        return None

    async def assemble(
        self,
        *,
        session_id: str,
        messages: list[AgentMessage],
        token_budget: int | None = None,
        session_key: str | None = None,
        available_tools: set[str] | None = None,
    ) -> AssembleResult:
        # Pass through. Token estimate is best-effort — `estimate_tokens`
        # uses tiktoken when present, char/3.5 fallback otherwise.
        total = sum(estimate_tokens(_message_text(m)) for m in messages)
        return AssembleResult(messages=list(messages), estimated_tokens=total)

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
        # Delegation kept here (rather than calling maybe_compact
        # directly) so an engine that subclasses LegacyContextEngine
        # can override `compact` without re-implementing the full path.
        from oxenclaw.pi.context_engine.delegate import delegate_compaction_to_runtime

        return await delegate_compaction_to_runtime(
            session_id=session_id,
            messages=messages,
            token_budget=token_budget,
            current_token_count=current_token_count,
            force=force,
        )


def _message_text(message: AgentMessage) -> str:
    """Best-effort serialization for token-counting.

    AgentMessage's content can be str, list of content blocks, or None
    depending on role. The legacy engine doesn't need exact tokens —
    just a sane estimate so callers can compare against budgets.
    """
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)
    return ""


def register_legacy_context_engine() -> None:
    """Register the legacy engine under `slot="legacy"` if not already.

    Idempotent: re-registration by the same owner is allowed so the
    init helper can call this on every gateway boot.
    """
    from oxenclaw.pi.context_engine.registry import register_context_engine_for_owner

    register_context_engine_for_owner(
        slot="legacy",
        owner="oxenclaw.pi.context_engine",
        factory=LegacyContextEngine,
    )


__all__ = ["LegacyContextEngine", "register_legacy_context_engine"]
