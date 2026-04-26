"""PiAgent — Agent Protocol bridge over the pi-embedded-runner stack.

This is the integration layer that lets the existing
`oxenclaw.agents.dispatch.Dispatcher` drive the new `oxenclaw.pi.run`
loop. It speaks the same `Agent` Protocol the dispatcher expects
(handle(inbound, ctx) → AsyncIterator[SendParams]) but internally:

1. Resolves a `Model` via `ModelRegistry` and an `Api` via `AuthStorage`.
2. Builds a system prompt via `system_prompt.assemble_system_prompt`.
3. Loads the persistent transcript from `SessionManager` (creates if missing).
4. Calls `run.run_agent_turn` with the transcript + tools + RuntimeConfig.
5. Persists the appended messages back via `SessionManager.save`.
6. Streams the final assistant text out as `SendParams` chunks.

Existing `LocalAgent` keeps working untouched — operators opt into the
pi pipeline by registering a `PiAgent` instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import oxenclaw.pi.providers  # noqa: F401  registers stream wrappers
from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from oxenclaw.pi import (
    AssistantMessage,
    AuthStorage,
    CreateAgentSessionOptions,
    EnvAuthStorage,
    InMemorySessionManager,
    Model,
    ModelRegistry,
    SessionManager,
    TextContent,
    UserMessage,
    default_registry,
    resolve_api,
)
from oxenclaw.pi.cache_observability import CacheObserver, should_apply_cache_markers
from oxenclaw.pi.compaction import maybe_compact, truncating_summarizer
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.system_prompt import (
    assemble_system_prompt,
    memory_contribution,
    skills_contribution,
)
from oxenclaw.pi.thinking import ThinkingLevel
from oxenclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from oxenclaw.plugin_sdk.reply_runtime import chunk_text
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.pi")


DEFAULT_SYSTEM_PROMPT = (
    "You are oxenClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)


class PiAgent:
    """Agent Protocol implementation backed by `oxenclaw.pi.run`."""

    def __init__(
        self,
        *,
        agent_id: str = "pi",
        model_id: str = "gemma4:latest",
        registry: ModelRegistry | None = None,
        auth: AuthStorage | None = None,
        sessions: SessionManager | None = None,
        tools: ToolRegistry | None = None,
        paths: OxenclawPaths | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        runtime: RuntimeConfig | None = None,
        memory: MemoryRetriever | None = None,
        memory_top_k: int = 5,
        include_skills: bool = True,
        chunk_limit: int = 4_000,
        thinking: ThinkingLevel | str | None = None,
        compact_keep_tail_turns: int = 6,
    ) -> None:
        self.id = agent_id
        self._registry = registry or default_registry()
        self._auth = auth or EnvAuthStorage()
        self._sessions = sessions or InMemorySessionManager()
        self._tools = tools or ToolRegistry()
        self._paths = paths or default_paths()
        self._system_prompt = system_prompt
        self._memory = memory
        self._memory_top_k = memory_top_k
        self._include_skills = include_skills
        self._chunk_limit = chunk_limit
        self._compact_keep_tail = compact_keep_tail_turns

        self._model: Model = self._registry.require(model_id)
        runtime = runtime or RuntimeConfig()
        if thinking is not None and runtime.thinking is None:
            runtime.thinking = thinking
        self._runtime = runtime

        # Per-session state: cache observer + sticky session id keyed by
        # the dispatcher's session_key.
        self._observers: dict[str, CacheObserver] = {}
        self._session_ids: dict[str, str] = {}

    # ─── system prompt assembly ─────────────────────────────────────

    async def _system_for(self, query: str) -> str:
        contributions = []
        if self._include_skills:
            block = format_skills_for_prompt(load_installed_skills(self._paths))
            if block:
                contributions.append(skills_contribution(skills_block=block))
        if self._memory is not None and query.strip():
            try:
                hits = await self._memory.search(query=query, k=self._memory_top_k)
                mblock = format_memories_for_prompt(hits)
                if mblock:
                    contributions.append(memory_contribution(memory_block=mblock))
            except Exception:
                logger.exception("memory recall failed")
        prompt, _ = assemble_system_prompt(self._system_prompt, contributions)
        return prompt

    # ─── session management ─────────────────────────────────────────

    async def _ensure_session(self, key: str) -> Any:
        sid = self._session_ids.get(key)
        if sid is not None:
            existing = await self._sessions.get(sid)
            if existing is not None:
                return existing
        s = await self._sessions.create(
            CreateAgentSessionOptions(
                agent_id=self.id,
                model_id=self._model.id,
                title=key,
            )
        )
        self._session_ids[key] = s.id
        return s

    # ─── Agent Protocol ─────────────────────────────────────────────

    async def handle(
        self, inbound: InboundEnvelope, ctx: AgentContext
    ) -> AsyncIterator[SendParams]:
        from oxenclaw.multimodal import (
            model_supports_images,
            normalize_inbound_images,
            pi_image_content,
        )

        text = (inbound.text or "").strip()

        # Resolve image attachments. If the model can't handle images, we
        # surface that as a text fallback so the model knows context was
        # lost rather than silently dropping.
        images: list = []
        dropped_notes: list[str] = []
        if inbound.media:
            if model_supports_images(self._model.id):
                images, dropped_notes = await normalize_inbound_images(inbound.media)
            else:
                photo_count = sum(1 for m in inbound.media if m.kind == "photo")
                if photo_count:
                    dropped_notes.append(
                        f"({photo_count} image(s) dropped: model "
                        f"{self._model.id!r} does not support image input)"
                    )

        if not text and not images and not dropped_notes:
            return

        # Build the user message content. Pure-text turns keep the
        # `content: str` shape (smaller payload, identical semantics);
        # mixed turns become a list of typed blocks.
        if images:
            blocks: list = [pi_image_content(img) for img in images]
            text_parts = [t for t in (text, *dropped_notes) if t]
            if text_parts:
                blocks.append(TextContent(text="\n".join(text_parts)))
            user_content: Any = blocks
        else:
            combined = "\n".join(t for t in (text, *dropped_notes) if t)
            user_content = combined

        session = await self._ensure_session(ctx.session_key)
        session.messages.append(UserMessage(content=user_content))

        api = await resolve_api(self._model, self._auth)
        system = await self._system_for(text)

        observer = self._observers.setdefault(session.id, CacheObserver())
        breakpoints = (
            self._runtime.cache_control_breakpoints if should_apply_cache_markers(observer) else 0
        )

        # Per-turn config snapshot — cache breakpoints adapted from observer.
        turn_runtime = RuntimeConfig(
            **{
                **self._runtime.__dict__,
                "cache_control_breakpoints": breakpoints,
            }
        )

        result = await run_agent_turn(
            model=self._model,
            api=api,
            system=system,
            history=session.messages,
            tools=list(self._tools._tools.values()),
            config=turn_runtime,
        )

        # Persist the appended messages back to the session.
        session.messages.extend(result.appended_messages)
        observer.record(result.usage_total or {})
        await maybe_compact(
            session,
            model_context_tokens=self._model.context_window,
            summarizer=truncating_summarizer,
            keep_tail_turns=self._compact_keep_tail,
        )
        await self._sessions.save(session)

        # Emit final text chunked for channel limits.
        if isinstance(result.final_message, AssistantMessage):
            text_parts = [
                b.text for b in result.final_message.content if isinstance(b, TextContent)
            ]
            reply = "\n".join(p for p in text_parts if p).strip()
            if not reply:
                return
            for chunk in chunk_text(reply, self._chunk_limit):
                yield SendParams(target=inbound.target, text=chunk)


__all__ = ["PiAgent"]
