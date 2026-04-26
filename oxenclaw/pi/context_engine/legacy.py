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
    CompactionTarget,
    CompactResult,
    ContextEngineInfo,
    ContextEngineMaintenanceResult,
    ContextEngineRuntimeContext,
    IngestBatchResult,
    IngestResult,
    SubagentEndReason,
    SubagentSpawnPreparation,
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
        return ContextEngineMaintenanceResult(changed=False)

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
        compaction_target: CompactionTarget = "threshold",
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> CompactResult:
        # Two paths:
        #  - Session-aware: PiAgent passes the live `AgentSession` via
        #    `runtime_context.extra["session"]`. We call `maybe_compact`
        #    on it so `session.messages` AND `session.compactions` are
        #    updated atomically — matches pre-rc.16 inline behavior.
        #  - Stateless: no session in context, fall back to the
        #    delegate path (decide → apply, returns the rewritten
        #    message list inside the CompactResult; caller is expected
        #    to splice it back).
        session = None
        summarizer = None
        keep_tail_turns: int | None = None
        if runtime_context is not None and isinstance(runtime_context.extra, dict):
            session = runtime_context.extra.get("session")
            summarizer = runtime_context.extra.get("summarizer")
            keep_tail_turns = runtime_context.extra.get("keep_tail_turns")
        if session is not None:
            from oxenclaw.pi.compaction import maybe_compact, truncating_summarizer

            compacted = await maybe_compact(
                session,
                model_context_tokens=token_budget or 32_000,
                summarizer=summarizer or truncating_summarizer,
                keep_tail_turns=keep_tail_turns or 6,
                force=force,
            )
            if not compacted:
                return CompactResult(
                    ok=True,
                    compacted=False,
                    reason="below threshold",
                )
            entry = session.compactions[-1]
            return CompactResult(
                ok=True,
                compacted=True,
                reason=entry.reason,
                summary=entry.summary or None,
                tokens_before=entry.tokens_before,
                tokens_after=entry.tokens_after,
            )
        from oxenclaw.pi.context_engine.delegate import delegate_compaction_to_runtime

        return await delegate_compaction_to_runtime(
            session_id=session_id,
            messages=messages,
            token_budget=token_budget,
            current_token_count=current_token_count,
            force=force,
        )

    async def prepare_subagent_spawn(
        self,
        *,
        parent_session_id: str,
        child_session_key: str,
        runtime_context: ContextEngineRuntimeContext | None = None,
    ) -> SubagentSpawnPreparation | None:
        return None

    async def on_subagent_ended(
        self,
        *,
        child_session_key: str,
        reason: SubagentEndReason,
    ) -> None:
        return None

    async def dispose(self) -> None:
        return None


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
