"""PiAgent — Agent Protocol bridge over the pi-embedded-runner stack.

This is the integration layer that lets the existing
`sampyclaw.agents.dispatch.Dispatcher` drive the new `sampyclaw.pi.run`
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

import sampyclaw.pi.providers  # noqa: F401  registers stream wrappers

from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.tools import ToolRegistry
from sampyclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from sampyclaw.pi import (
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
from sampyclaw.pi.cache_observability import CacheObserver, should_apply_cache_markers
from sampyclaw.pi.compaction import maybe_compact, truncating_summarizer
from sampyclaw.pi.run import RuntimeConfig, run_agent_turn
from sampyclaw.pi.system_prompt import (
    assemble_system_prompt,
    memory_contribution,
    skills_contribution,
)
from sampyclaw.pi.thinking import ThinkingLevel
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from sampyclaw.plugin_sdk.reply_runtime import chunk_text
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.pi")


DEFAULT_SYSTEM_PROMPT = (
    "You are sampyClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)


class PiAgent:
    """Agent Protocol implementation backed by `sampyclaw.pi.run`."""

    def __init__(
        self,
        *,
        agent_id: str = "pi",
        model_id: str = "gemma4:latest",
        registry: ModelRegistry | None = None,
        auth: AuthStorage | None = None,
        sessions: SessionManager | None = None,
        tools: ToolRegistry | None = None,
        paths: SampyclawPaths | None = None,
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
        text = (inbound.text or "").strip()
        if not text:
            return

        session = await self._ensure_session(ctx.session_key)
        session.messages.append(UserMessage(content=text))

        api = await resolve_api(self._model, self._auth)
        system = await self._system_for(text)

        observer = self._observers.setdefault(session.id, CacheObserver())
        breakpoints = (
            self._runtime.cache_control_breakpoints
            if should_apply_cache_markers(observer)
            else 0
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
