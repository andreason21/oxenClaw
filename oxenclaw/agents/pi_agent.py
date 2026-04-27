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
6. Mirrors the user/assistant turn into `ConversationHistory` so the
   dashboard's `chat.history` poll can render it (the pi SessionManager
   stores a richer transcript the runner needs; the dashboard wants a
   flat role/content list — keep both).
7. Streams the final assistant text out as `SendParams` chunks.

Existing `LocalAgent` keeps working untouched — operators opt into the
pi pipeline by registering a `PiAgent` instead.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import oxenclaw.pi.providers  # noqa: F401  registers stream wrappers
from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.agents.incomplete_turn import repair_incomplete_turn
from oxenclaw.agents.message_merge import merge_consecutive_same_role
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.memory.active import (
    ActiveMemoryConfig,
    ActiveMemoryRunner,
    format_active_memory_prelude,
)
from oxenclaw.memory.retriever import (
    MemoryRetriever,
    format_memories_as_prelude,
    format_memories_for_prompt,
)
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
    ToolUseBlock,
    UserMessage,
    default_registry,
    resolve_api,
)
from oxenclaw.pi.cache_observability import CacheObserver, should_apply_cache_markers
from oxenclaw.pi.compaction import (
    CompactionGuard,
    llm_structured_summarizer,
    truncating_summarizer,
)
from oxenclaw.pi.context_engine import (
    ContextEngine,
    ContextEngineRuntimeContext,
    LegacyContextEngine,
)
from oxenclaw.pi.hooks import HookContext, HookRunner
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.run.history_image_prune import prune_old_images
from oxenclaw.pi.system_prompt import (
    assemble_system_prompt,
    embedded_context_contribution,
    execution_bias_contribution,
    load_project_context_files,
    memory_contribution,
    memory_recall_contribution,
    skills_contribution,
    skills_mandatory_contribution,
)
from oxenclaw.pi.thinking import ThinkingLevel
from oxenclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from oxenclaw.plugin_sdk.reply_runtime import chunk_text
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.pi")


DEFAULT_SYSTEM_PROMPT = (
    "You are oxenClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful.\n"
    "\n"
    "Time + freshness. You do NOT know the current date or time without\n"
    "calling a tool. If the user asks about \"now\", \"today\", \"이번 주\",\n"
    "\"지금\", or any question whose answer depends on the current\n"
    "date/time, call `get_time` first. Never guess the date.\n"
    "\n"
    "Memory playbook. Long-term facts about the user / project / past\n"
    "decisions live in a vector-indexed memory store with two tiers:\n"
    "the raw `inbox` (everything you save) and the curated `short_term`\n"
    "tier (durable facts you've explicitly promoted).\n"
    "  - `memory_save(text=\"...\", tags=[\"...\"])` — append a stable fact\n"
    "    to the inbox whenever the user asks you to remember something\n"
    "    OR you learn a durable preference (their name, role, deadline,\n"
    "    tooling preference). Rules of thumb for `text`:\n"
    "      * Write a COMPLETE natural-language sentence, not a\n"
    "        `key:value` line. \"사용자는 수원에 거주한다. The user lives\n"
    "        in Suwon, South Korea.\" beats `user_location:Suwon`.\n"
    "      * Include BOTH the user's language and English when the\n"
    "        user wrote in a non-English language — same fact, two\n"
    "        phrasings. The embedding store hits cross-language\n"
    "        queries that way (\"내가 사는 곳 날씨\" / \"weather where I\n"
    "        live\" both surface the chunk).\n"
    "    Arg names are exactly `text` (string) and `tags` (list of\n"
    "    strings). Common-alias forms (`content`, `body`, `note`,\n"
    "    `key`, `category`, `tag`) are also accepted but `text`+`tags`\n"
    "    is the canonical shape — prefer it. Skip ephemeral chat-\n"
    "    transcript details.\n"
    "  - `memory_search(query, k?)` — explicit recall when the auto-\n"
    "    injected memory block at prompt-time didn't surface what you\n"
    "    need (e.g. \"what did I tell you about X last week?\",\n"
    "    \"remember my deadline?\").\n"
    "  - When the user explicitly says \"this is important, don't\n"
    "    forget\" or you've verified a fact across multiple turns, use\n"
    "    `memory.promote` (RPC) to lift the inbox chunk into\n"
    "    `short_term` with a confidence score + tags. Operators see\n"
    "    promoted facts highlighted in the Memory dashboard tab.\n"
    "\n"
    "Skill discovery. The system prompt's `<available_skills>` block\n"
    "lists installed skills as documentation, not callable tools. If\n"
    "the user's request implies a domain you have no listed skill for\n"
    "(weather / stock prices / calendar / etc.), call\n"
    "`skill_resolver(query=\"...\")` — it searches ClawHub, installs the\n"
    "best match, and returns the SKILL.md path + how to invoke it via\n"
    "the shell tool. Don't refuse before trying skill_resolver.\n"
    "\n"
    "Weather playbook. For weather / temperature / forecast questions\n"
    "(\"날씨\", \"weather\", \"temperature\", \"forecast\", \"비 와?\")\n"
    "ALWAYS prefer the dedicated `weather` tool — do NOT use web_search\n"
    "for weather. Required arg: `city` (string) OR `lat`+`lon` (numbers).\n"
    "  - If the user named a city: `weather(city=\"<city>\")`.\n"
    "  - If they didn't: check the recalled-memories block first for a\n"
    "    location fact (e.g. \"User lives in Suwon\") and use that. Only\n"
    "    if neither the question nor recall reveals a location, ask the\n"
    "    user once: \"어느 도시 날씨를 알려드릴까요?\".\n"
    "  - The tool returns one short line (e.g. `Suwon: 🌦 +18°C`); read\n"
    "    it back to the user. wttr.in is the upstream provider — it\n"
    "    rarely returns errors so don't fall back to web_search after\n"
    "    one weather-tool call.\n"
    "\n"
    "Web research playbook (mirrors openclaw's chaining guide). When the\n"
    "user asks a factual / current-events / market-research question and\n"
    "you need fresh information:\n"
    "  1. Try `web_search` first. The result is a ranked URL list.\n"
    "  2. If it returns 0 hits OR the snippets aren't enough to answer,\n"
    "     do NOT give up — pick the best matching URL (or a known\n"
    "     authoritative source) and call `web_fetch` to load the actual\n"
    "     page body. Repeat with another URL if the first is 404 / off-\n"
    "     topic. A 404 from web_fetch is data, not a stopping signal.\n"
    "  3. Try alternate query phrasings (English / Korean / domain\n"
    "     site: filters) before reporting that nothing was found.\n"
    "  4. When you do answer from web data, cite the URLs you fetched.\n"
    "  5. NEVER use web_search for queries that have a dedicated tool\n"
    "     (weather → `weather`, github → `github`, current time →\n"
    "     `get_time`). Reach for the specialised tool first.\n"
    "\n"
    "Wiki playbook. The wiki vault stores *durable* knowledge that\n"
    "survives across many sessions (decisions, entities, concepts,\n"
    "sources). Two rules:\n"
    "  - When the user asks 'what do you know about X?' or 'remember\n"
    "    our decision on Y', call `wiki_search` first. It returns pages\n"
    "    from the vault ranked by keyword overlap.\n"
    "  - If nothing matches AND the user is sharing a new authoritative\n"
    "    claim or decision, propose `wiki_save` — explain the page you\n"
    "    would create (kind, title, body) before calling it.\n"
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
        memory_weak_threshold: float = 0.30,
        memory_inject_into_user: bool = True,
        active_memory: ActiveMemoryConfig | None = None,
        include_skills: bool = True,
        include_execution_bias: bool = True,
        project_context_dir: Path | str | None = None,
        chunk_limit: int = 4_000,
        thinking: ThinkingLevel | str | None = None,
        compact_keep_tail_turns: int = 6,
        context_engine: ContextEngine | None = None,
        hooks: HookRunner | None = None,
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
        self._memory_weak_threshold = memory_weak_threshold
        self._memory_inject_into_user = memory_inject_into_user
        self._active_memory_config = active_memory or ActiveMemoryConfig(enabled=False)
        self._active_runner: ActiveMemoryRunner | None = None
        self._include_skills = include_skills
        self._include_execution_bias = include_execution_bias
        self._project_context_dir: Path | None = (
            Path(project_context_dir) if project_context_dir is not None else None
        )
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
        # Frozen recall snapshot per session — captured once when the
        # session is first seen so the cacheable portion of the system
        # prompt stays byte-stable across turns. Dynamic per-query
        # recall still flows through `memory_contribution` below the
        # cache marker, but this snapshot adds query-independent user
        # facts to the cached prefix without invalidating it on writes.
        # Keyed by (session_key) → frozen XML block string.
        self._recall_snapshots: dict[str, str] = {}

        # Context engine — defaults to the pass-through `LegacyContextEngine`
        # so behaviour matches pre-rc.16 PiAgent byte-for-byte. Operators
        # opt into a custom strategy by injecting it here.
        self._engine: ContextEngine = context_engine or LegacyContextEngine()

        # Hook runner — empty by default. Operators populate before/after
        # tool / on_empty_reply / on_turn_end callbacks via constructor.
        self._hooks: HookRunner = hooks or HookRunner()

        # Iterative-summary state for the structured LLM-based
        # summariser pipeline. Keyed by session_key.
        # Each entry is a tuple (prior_summary_text, CompactionGuard).
        self._compaction_state: dict[str, tuple[str | None, CompactionGuard]] = {}

    # ─── user-side recall (the strongest injection point) ─────────

    async def _active_memory_prelude(
        self, query: str, recent_messages: list[Any], session_key: str
    ) -> str:
        """Run the active-memory sub-agent if enabled and return the
        rendered prelude (or "")."""
        if (
            self._memory is None
            or not self._active_memory_config.enabled
        ):
            return ""
        if self._active_runner is None:
            self._active_runner = ActiveMemoryRunner(
                memory=self._memory,
                main_model=self._model,
                api_resolver=lambda m: resolve_api(m, self._auth),
                config=self._active_memory_config,
            )
        # Hot-swap sub-model lazily if config asks for a different one.
        if self._active_memory_config.model_id and (
            self._active_runner.sub_model is None
            or self._active_runner.sub_model.id != self._active_memory_config.model_id
        ):
            try:
                self._active_runner.sub_model = self._registry.require(
                    self._active_memory_config.model_id
                )
            except KeyError:
                logger.warning(
                    "active-memory model %r not registered; falling back to main",
                    self._active_memory_config.model_id,
                )
                self._active_runner.sub_model = None
        summary = await self._active_runner.recall_for_turn(
            query=query,
            recent_messages=recent_messages,
            session_key=session_key,
        )
        return format_active_memory_prelude(summary)

    async def _build_user_recall_prelude(self, query: str) -> str:
        """Run a recall search and render it as a tight prelude that
        will be prepended to the user message body for the model.

        Why user-side and not system-side? Small local models
        (gemma2/3, qwen2.5:3b, llama3.1:8b) consistently ignore long
        system-prompt context but strongly attend to the user message.
        openclaw avoids the issue by relying on the model to call
        `memory_search` as a tool — the tool_result is part of the
        normal turn flow and gets full attention. We do BOTH so that
        models that don't yet know to call `memory_search` still
        benefit from prior context. The dashboard view never includes
        this prelude — only the model sees it.
        """
        if self._memory is None:
            return ""
        from oxenclaw.memory.hybrid import HybridConfig
        from oxenclaw.memory.temporal_decay import TemporalDecayConfig

        hits = await self._memory.search(
            query=query,
            k=self._memory_top_k,
            hybrid=HybridConfig(enabled=True),
            temporal_decay=TemporalDecayConfig(enabled=True),
        )
        if not hits:
            return ""
        prelude = format_memories_as_prelude(hits)
        if prelude:
            top = hits[0]
            logger.info(
                "user-side recall injected: %d hit(s) top=%.3f citation=%s",
                len(hits),
                top.score,
                top.citation,
            )
        return prelude

    # ─── summariser selection ───────────────────────────────────────

    def _build_summarizer(self, session_key: str):
        """Pick the active summariser for this session.

        When `RuntimeConfig.auxiliary_llm` is set, returns an adapter
        that drives the LLM-based structured summariser pipeline (keeps
        per-session prior_summary + CompactionGuard so iterative
        compactions preserve information). Falls back to the cheap
        `truncating_summarizer` otherwise — byte-for-byte default
        behaviour for callers that didn't opt in.
        """
        aux_llm = getattr(self._runtime, "auxiliary_llm", None)
        if aux_llm is None:
            return truncating_summarizer

        async def _summarize(messages):
            prior, guard = self._compaction_state.get(
                session_key, (None, CompactionGuard())
            )
            try:
                summary = await llm_structured_summarizer(
                    messages,
                    summarizer_llm=aux_llm,
                    prior_summary=prior,
                )
            except Exception:
                logger.exception("auxiliary_llm summariser failed; falling back")
                return await truncating_summarizer(messages)
            if summary:
                self._compaction_state[session_key] = (summary, guard)
            return summary or ""

        return _summarize

    # ─── runtime model swap (debug / A-B) ──────────────────────────

    def set_model_id(self, model_id: str) -> str:
        """Hot-swap the underlying Model. Returns the new model id.

        Used by the `agents.set_model` RPC for A/B testing recall
        attention across local models without restarting the gateway.
        Cache observers are keyed by session id and store provider-
        specific cache markers, so we drop them on swap to avoid
        sending stale cache_control breakpoints to a different
        provider/model.
        """
        new_model = self._registry.require(model_id)
        if new_model.id == self._model.id:
            return new_model.id
        self._model = new_model
        self._observers.clear()
        logger.info("agents.set_model: %s now using model %s", self.id, new_model.id)
        return new_model.id

    # ─── system prompt assembly ─────────────────────────────────────

    async def _system_for(self, query: str, *, session_key: str | None = None) -> str:
        prompt, _hits, _mblock, _skills_block = await self._assemble_system(
            query, session_key=session_key,
        )
        return prompt

    async def debug_assemble(
        self, query: str, *, session_key: str | None = None
    ) -> dict[str, Any]:
        """Return the same artefacts a real turn would build, for the
        `chat.debug_prompt` RPC. We never want a debug-RPC failure to
        break the gateway, so each step is isolated."""
        prompt, hits, mblock, skills_block = await self._assemble_system(
            query, session_key=session_key,
        )
        prelude = format_memories_as_prelude(hits)
        hit_payload = [
            {
                "chunk_id": h.chunk.id,
                "citation": h.citation,
                "score": float(h.score),
                "distance": float(h.distance),
                "path": h.chunk.path,
                "start_line": h.chunk.start_line,
                "end_line": h.chunk.end_line,
                "text_preview": h.chunk.text.strip().replace("\n", " ")[:240],
            }
            for h in hits
        ]
        return {
            "model_id": self._model.id,
            "agent_id": self.id,
            "system_prompt": prompt,
            "system_prompt_chars": len(prompt),
            "base_prompt_chars": len(self._system_prompt),
            "memory_hits": hit_payload,
            "memory_block": mblock,
            "memory_block_chars": len(mblock),
            "memory_prelude": prelude,
            "memory_prelude_chars": len(prelude),
            "memory_weak_threshold": self._memory_weak_threshold,
            "skills_block": skills_block,
            "skills_block_chars": len(skills_block),
        }

    async def _ensure_recall_snapshot(self, session_key: str) -> str:
        """Capture a frozen recall block once per session.

        Hermes-agent style: `tools/memory_tool.py:_system_prompt_snapshot`
        — mid-session writes mutate the live store but the prompt-
        injected snapshot does NOT change, preserving the provider's
        prompt cache for the entire session. We adopt the same pattern
        here for query-independent user facts (no specific query →
        general "what we already know about this user"); per-turn
        query-specific recall continues to flow through the
        `cacheable=False` `memory_contribution` slot.
        """
        cached = self._recall_snapshots.get(session_key)
        if cached is not None:
            return cached
        if self._memory is None:
            self._recall_snapshots[session_key] = ""
            return ""
        try:
            from oxenclaw.memory.hybrid import HybridConfig
            from oxenclaw.memory.temporal_decay import TemporalDecayConfig
            # Generic "user / preferences / identity" probe — pulls the
            # most recall-worthy long-lived facts. Empty / weak result
            # is OK; we still want to freeze "" for the session so a
            # later memory_save mid-session doesn't bust the cache.
            hits = await self._memory.search(
                query="user identity preferences personal facts",
                k=self._memory_top_k,
                hybrid=HybridConfig(enabled=True),
                temporal_decay=TemporalDecayConfig(enabled=True),
            )
        except Exception:
            logger.exception("recall snapshot probe failed")
            hits = []
        block = format_memories_for_prompt(hits) if hits else ""
        self._recall_snapshots[session_key] = block
        if block:
            logger.info(
                "recall snapshot frozen for session=%s hits=%d",
                session_key, len(hits),
            )
        return block

    def invalidate_recall_snapshot(self, session_key: str | None = None) -> None:
        """Drop the frozen recall snapshot.

        Called on explicit session reset / fork. Mid-session memory_save
        deliberately does NOT call this — the whole point of the
        snapshot is that writes during a session don't invalidate the
        cached prompt prefix. The new snapshot is taken at the start
        of the *next* session.
        """
        if session_key is None:
            self._recall_snapshots.clear()
        else:
            self._recall_snapshots.pop(session_key, None)

    async def _assemble_system(
        self, query: str, *, session_key: str | None = None
    ) -> tuple[str, list, str, str]:
        contributions = []
        # Universal "keep going / verify with tools" block — static text,
        # cacheable, applies regardless of skill / memory state. Ported
        # from openclaw `buildExecutionBiasSection`.
        if self._include_execution_bias:
            contributions.append(execution_bias_contribution())
        # Project context (AGENTS.md / SOUL.md / etc.) — opt-in via
        # `project_context_dir`. Cacheable; only changes when files do.
        if self._project_context_dir is not None:
            files_block = load_project_context_files(self._project_context_dir)
            if files_block:
                contributions.append(
                    embedded_context_contribution(files_block=files_block)
                )
        skills_block = ""
        if self._include_skills:
            skills_block = format_skills_for_prompt(load_installed_skills(self._paths))
            if skills_block:
                # Procedure block first (priority 18), then the XML
                # `<available_skills>` block (priority 20). Same layout
                # as openclaw `buildSkillsSection`.
                contributions.append(skills_mandatory_contribution())
                contributions.append(skills_contribution(skills_block=skills_block))
        # Frozen session recall snapshot — adds query-independent user
        # facts to the cacheable portion of the system prompt. Stays
        # byte-stable for the whole session so prompt-cache prefix
        # survives mid-session memory writes.
        if session_key is not None and self._memory is not None:
            snapshot = await self._ensure_recall_snapshot(session_key)
            if snapshot:
                from oxenclaw.pi.system_prompt import SystemPromptContribution
                contributions.append(
                    SystemPromptContribution(
                        name="recall_snapshot",
                        body=snapshot,
                        # Priority 25 sits between embedded_context (30) and
                        # skills (20) so a SKILL.md still lands at the top.
                        priority=25,
                        cacheable=True,
                    )
                )

        hits: list = []
        mblock = ""
        prelude = ""
        if self._memory is not None and query.strip():
            try:
                # Hybrid search by default: vector similarity alone misses
                # short key:value chunks ("user_location:Suwon") against
                # conversational queries ("내가 사는 곳 날씨"). FTS5 keyword
                # match is a backstop that surfaces those by token overlap
                # ("Suwon"/"수원" both appear in inbox + query). Cross-
                # language recall (KO ↔ EN) also benefits from the keyword
                # arm. Same defaults openclaw ships.
                from oxenclaw.memory.hybrid import HybridConfig
                from oxenclaw.memory.temporal_decay import TemporalDecayConfig
                hits = await self._memory.search(
                    query=query,
                    k=self._memory_top_k,
                    hybrid=HybridConfig(enabled=True),
                    temporal_decay=TemporalDecayConfig(enabled=True),
                )
                # Operator-facing visibility: log the hit count + top
                # score + per-hit scores + first chunk's citation so a
                # `tail -f` of gateway.log shows whether recall actually
                # surfaced something for this turn. Without this an empty
                # `<recalled_memories>` block (the "agent doesn't
                # remember" symptom) is invisible from logs alone.
                if hits:
                    top = hits[0]
                    score_breakdown = ",".join(f"{h.score:.2f}" for h in hits)
                    logger.info(
                        "memory recall: %d hit(s), top score=%.3f citation=%s scores=[%s] query=%r",
                        len(hits),
                        top.score,
                        top.citation,
                        score_breakdown,
                        query[:80],
                    )
                    if top.score < self._memory_weak_threshold:
                        # All hits are weak — log a warning but still
                        # include the block. The model can ignore weak
                        # context, and pulling the block hides the
                        # signal entirely. Operators see this in the log
                        # and can tune `memory_weak_threshold` per
                        # embedding model (different models score the
                        # same chunk pair differently).
                        logger.warning(
                            "memory recall: top score %.3f below %.2f — recall is weak for query=%r",
                            top.score,
                            self._memory_weak_threshold,
                            query[:80],
                        )
                else:
                    logger.info("memory recall: 0 hits for query=%r", query[:80])
                mblock = format_memories_for_prompt(hits)
                if mblock:
                    # Procedural rule (priority 70) above the XML block
                    # (priority 80) so the model reads the citation rule
                    # before the data. Mirrors openclaw's
                    # `buildMemoryPromptSection`.
                    contributions.append(memory_recall_contribution())
                    contributions.append(memory_contribution(memory_block=mblock))
                # Prelude — tight plain-text bullet list prepended to
                # the base prompt below. Covers small local models
                # whose attention fades before the XML block lower
                # down. We still emit the XML block (citation-aware
                # large models use it).
                prelude = format_memories_as_prelude(hits)
            except Exception:
                logger.exception("memory recall failed")
        # Prepend the recall prelude to the base — `assemble_system_prompt`
        # always anchors `base` at index 0, so this is the only way to put
        # something physically ABOVE the playbook.
        base = self._system_prompt
        if prelude:
            base = f"{prelude}\n\n{base}"
        prompt, _ = assemble_system_prompt(base, contributions)
        return prompt, hits, mblock, skills_block

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
        # Rehydrate from the dashboard-side ConversationHistory file.
        # `InMemorySessionManager` is process-local — every gateway
        # restart starts with an empty pi transcript, but the dashboard
        # poll keeps showing the prior turns from disk. That mismatch
        # is what made the agent "forget the conversation" — it really
        # WAS receiving an empty history list. Now we seed the new
        # session with whatever the on-disk ConversationHistory has
        # so cross-restart continuity matches what the user sees.
        try:
            history_path = self._paths.session_file(self.id, key)
            if history_path.exists():
                hist = ConversationHistory(history_path)
                seeded = 0
                for msg in hist.messages():
                    role = msg.get("role")
                    content = msg.get("content") or ""
                    if not content:
                        continue
                    if role == "user":
                        s.messages.append(UserMessage(content=content))
                        seeded += 1
                    elif role == "assistant":
                        s.messages.append(
                            AssistantMessage(
                                content=[TextContent(text=content)],
                                stop_reason="end_turn",
                            )
                        )
                        seeded += 1
                if seeded:
                    logger.info(
                        "pi session rehydrated from disk: agent=%s "
                        "session=%s seeded=%d",
                        self.id, key, seeded,
                    )
        except Exception:
            logger.exception("pi session rehydrate failed for %s/%s", self.id, key)
        # Repair incomplete-turn state (orphan tool_use, trailing user)
        # so the model never sees a corrupt transcript on the first
        # turn after a crash / restart.
        try:
            repair_incomplete_turn(s.messages)
        except Exception:
            logger.exception("incomplete-turn repair failed for %s/%s", self.id, key)
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

        # before_agent_reply hook — last chance to short-circuit the
        # whole turn (e.g. a cron handler that already knows the
        # answer, or a content filter that wants to refuse early).
        hook_ctx = HookContext(
            agent_id=self.id,
            session_key=ctx.session_key,
            workspace_dir=str(self._paths.home),
            model_provider=getattr(self._model, "provider", None),
            model_id=self._model.id,
            channel=inbound.channel,
        )
        early = await self._hooks.run_before_agent_reply(text, hook_ctx)
        if early is not None and early.handled:
            reply = (early.reply_text or "").strip()
            if not reply:
                return
            dashboard_history = ConversationHistory(
                self._paths.session_file(self.id, ctx.session_key)
            )
            dashboard_history.append({"role": "user", "content": text})
            dashboard_history.append({"role": "assistant", "content": reply})
            dashboard_history.save()
            for chunk in chunk_text(reply, self._chunk_limit):
                yield SendParams(target=inbound.target, text=chunk)
            await self._hooks.run_on_turn_end(reply, hook_ctx)
            return

        # User-side recall injection. The strongest place to put recalled
        # memories for a small local model is INSIDE the user message —
        # not the system prompt. The model treats the user message as
        # "the thing the human just said" and attends to it strongly,
        # while system-prompt blocks (especially long ones) get faded
        # out by gemma2/3 + qwen2.5:3b + llama3.1:8b. openclaw side-steps
        # this entirely by relying on the model to call `memory_search`
        # as a tool and reading the tool_result; we do BOTH (auto-inject
        # for tiny models, plus the tool for larger ones).
        #
        # The dashboard always shows what the user actually typed, NOT
        # the augmented version — the prelude is internal context.
        # Anti-replay sanitisation: strip any `<recalled_memories>` or
        # `<memory-context>` fence tags the user (or a quoted prior
        # assistant turn) might paste in. The fence tag is the structural
        # signal that "the bytes inside are authoritative recall" — if a
        # user echoes one back, downstream injection paths could re-
        # ingest it as ground truth. Tag bodies stay; only the open/close
        # tags are removed so the user's own words aren't lost.
        from oxenclaw.memory.privacy import sanitize_recall_fence
        sanitized_text = sanitize_recall_fence(text)
        user_text_raw = "\n".join(t for t in (sanitized_text, *dropped_notes) if t)
        user_text_for_model = user_text_raw

        # Active memory sub-agent — produces a one-line natural-language
        # summary that small models actually attend to (vs raw chunks
        # they tend to ignore). Only fires when the user has opted in
        # via active_memory.enabled = True.
        if self._memory is not None and self._active_memory_config.enabled and text:
            try:
                # Use the existing pi-format session messages for "recent" context.
                # We haven't created the session yet at this point; check the
                # transcript file directly for a cheap recent-history view.
                session_for_active = await self._ensure_session(ctx.session_key)
                active_prelude = await self._active_memory_prelude(
                    text,
                    list(session_for_active.messages),
                    ctx.session_key,
                )
            except Exception:
                logger.exception("active-memory prelude failed")
                active_prelude = ""
            if active_prelude:
                user_text_for_model = f"{active_prelude}\n\n{user_text_raw}"

        if self._memory is not None and self._memory_inject_into_user and text:
            try:
                inject_prelude = await self._build_user_recall_prelude(text)
            except Exception:
                logger.exception("user-side recall prelude failed")
                inject_prelude = ""
            if inject_prelude:
                user_text_for_model = f"{inject_prelude}\n\n{user_text_for_model}"

        # Build the user message content. Pure-text turns keep the
        # `content: str` shape (smaller payload, identical semantics);
        # mixed turns become a list of typed blocks.
        if images:
            blocks: list = [pi_image_content(img) for img in images]
            if user_text_for_model:
                blocks.append(TextContent(text=user_text_for_model))
            user_content: Any = blocks
        else:
            user_content = user_text_for_model

        session = await self._ensure_session(ctx.session_key)
        session.messages.append(UserMessage(content=user_content))

        # Parallel ConversationHistory write so the dashboard's `chat.history`
        # poll surfaces this turn. PiAgent's pi-format SessionManager and the
        # dashboard-format ConversationHistory are intentionally separate
        # stores — pi owns the rich transcript the runner needs, while the
        # dashboard wants a flat role/content list. The dashboard sees the
        # ORIGINAL user text (what they typed), not the recall-augmented
        # version — operator UX shouldn't leak the synthetic prelude.
        dashboard_history = ConversationHistory(
            self._paths.session_file(self.id, ctx.session_key)
        )
        dashboard_history.append({"role": "user", "content": user_text_raw or "(image)"})
        dashboard_history.save()

        # Engine ingest — single-message hook for the user turn just
        # appended. LegacyContextEngine no-ops; custom engines can index
        # the message into their own store before the model call.
        await self._engine.ingest(
            session_id=session.id,
            session_key=ctx.session_key,
            message=session.messages[-1],
        )

        api = await resolve_api(self._model, self._auth)
        system = await self._system_for(text, session_key=ctx.session_key)

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

        # Engine assemble — lets custom engines reshape the history (e.g.
        # active-memory injects retrieved memories) and prepend extra
        # system-prompt content. Legacy passes through unchanged.
        assembled = await self._engine.assemble(
            session_id=session.id,
            session_key=ctx.session_key,
            messages=session.messages,
            token_budget=self._model.context_window,
            available_tools=set(self._tools._tools.keys()),
        )
        history = assembled.messages
        if assembled.system_prompt_addition:
            system = f"{assembled.system_prompt_addition}\n\n{system}"

        pre_prompt_count = len(session.messages)

        # History image prune — replace old base64 image blobs with
        # placeholders so a 30-turn multimodal session doesn't blow
        # the context window. Always safe; defaults keep the most
        # recent 2 user turns intact.
        try:
            prune_old_images(history, keep_recent_user_turns=2)
        except Exception:
            logger.exception("history-image-prune failed (non-fatal)")

        # Merge consecutive same-role messages — Anthropic + Google
        # reject them; small models attend to the merged single turn
        # better than two adjacent fragments anyway.
        try:
            merge_consecutive_same_role(history)
        except Exception:
            logger.exception("message-merge failed (non-fatal)")

        ts_turn_start = time.time()
        # Wire hooks into the per-turn runtime so before/after_tool_use
        # fire from inside the run loop. Reuse the same RuntimeConfig
        # but copy so we don't mutate the agent's stored config.
        turn_runtime = RuntimeConfig(
            **{
                **turn_runtime.__dict__,
                "hook_runner": self._hooks,
                "hook_context": hook_ctx,
            }
        )
        result = await run_agent_turn(
            model=self._model,
            api=api,
            system=system,
            history=history,
            tools=list(self._tools._tools.values()),
            config=turn_runtime,
        )

        # Persist the appended messages back to the session.
        session.messages.extend(result.appended_messages)
        observer.record(result.usage_total or {})

        # Persist cumulative usage for `usage.session` / `usage.totals` RPCs.
        # Cost is computed from the model's `pricing` map (USD per million
        # tokens, keyed by usage-dict field). Written as a sibling file so
        # ConversationHistory's schema stays unchanged.
        self._persist_usage(ctx.session_key)

        # Engine ingest_batch + after_turn — give the engine the new
        # messages from this turn as a unit so it can index / persist /
        # decide on background work.
        if result.appended_messages:
            await self._engine.ingest_batch(
                session_id=session.id,
                session_key=ctx.session_key,
                messages=result.appended_messages,
            )
        await self._engine.after_turn(
            session_id=session.id,
            session_key=ctx.session_key,
            messages=session.messages,
            pre_prompt_message_count=pre_prompt_count,
            token_budget=self._model.context_window,
        )

        # Engine compact — pass the session through `runtime_context.extra`
        # so `LegacyContextEngine.compact` can mutate it via the existing
        # `maybe_compact` path. Custom engines that own their own message
        # store ignore the session and operate on `messages`.
        active_summarizer = self._build_summarizer(ctx.session_key)
        await self._engine.compact(
            session_id=session.id,
            messages=session.messages,
            token_budget=self._model.context_window,
            runtime_context=ContextEngineRuntimeContext(
                extra={
                    "session": session,
                    "summarizer": active_summarizer,
                    "keep_tail_turns": self._compact_keep_tail,
                }
            ),
        )

        await self._sessions.save(session)

        # Build a lookup of tool execution results keyed by tool-use id so we
        # can correlate ToolUseBlock entries in appended messages with their
        # timing data. `result.tool_executions` is populated by the run loop;
        # use getattr() because some test mocks return a SimpleNamespace
        # that omits the field.
        exec_by_id = {
            e.id: e for e in (getattr(result, "tool_executions", None) or [])
        }

        # Walk appended messages to persist tool-call timing into the dashboard
        # history. We use `ts_turn_start` (wall clock captured before the run)
        # as the anchor; per-tool offsets are derived from `duration_seconds`
        # in ToolExecutionResult (monotonic, sequential within the turn). Since
        # pi-runtime does not expose before/after_tool hooks, we reconstruct
        # wall-clock start/end by accumulating durations in the order tools
        # executed. Tool results come from result.tool_executions, not from
        # ToolResultMessage blocks, so we don't need to parse the message list
        # for result content.
        accumulated_seconds = 0.0
        for msg in result.appended_messages:
            if not isinstance(msg, AssistantMessage):
                continue
            tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
            if not tool_uses:
                continue
            tool_calls_payload: list[dict[str, Any]] = []
            for tu in tool_uses:
                exec_result = exec_by_id.get(tu.id)
                dur = exec_result.duration_seconds if exec_result else 0.0
                ts_start = ts_turn_start + accumulated_seconds
                ts_end = ts_start + dur
                accumulated_seconds += dur
                status = "error" if (exec_result and exec_result.is_error) else "ok"
                raw_output = (exec_result.output if exec_result else "") or ""
                tool_calls_payload.append(
                    {
                        "id": tu.id,
                        "name": tu.name,
                        "args": tu.input,
                        "started_at": ts_start,
                        "ended_at": ts_end,
                        "status": status,
                        "output_preview": raw_output[:200],
                    }
                )
            text_parts_tc = [b.text for b in msg.content if isinstance(b, TextContent)]
            assistant_text_tc = "\n".join(p for p in text_parts_tc if p).strip()
            dashboard_history.append(
                {
                    "role": "assistant",
                    "content": assistant_text_tc,
                    "tool_calls": tool_calls_payload,
                }
            )
        dashboard_history.save()

        # Emit final text chunked for channel limits.
        if isinstance(result.final_message, AssistantMessage):
            text_parts = [
                b.text for b in result.final_message.content if isinstance(b, TextContent)
            ]
            reply = "\n".join(p for p in text_parts if p).strip()
            tool_uses_in_final = [
                b for b in result.final_message.content if isinstance(b, ToolUseBlock)
            ]
            if not reply:
                # Empty model output. Two scenarios:
                #   (a) the model emitted only tool_use blocks → already
                #       written to dashboard above; the final pass yielding
                #       no text is normal post-tool flow that the next turn
                #       resolves.
                #   (b) the model genuinely emitted nothing — rare on big
                #       models, common on small local models when a
                #       restrictive system prompt confuses them. Surface a
                #       diagnostic so dashboards don't show "the user sent
                #       a message and nothing happened" silently.
                logger.warning(
                    "pi turn produced no text reply: agent=%s session=%s "
                    "tool_uses_in_final=%d block_kinds=%s",
                    self.id,
                    ctx.session_key,
                    len(tool_uses_in_final),
                    [type(b).__name__ for b in result.final_message.content],
                )
                if not tool_uses_in_final:
                    # on_empty_reply hook — gives operators a last
                    # chance to substitute a useful reply before the
                    # default placeholder fires.
                    hook_text = await self._hooks.run_on_empty_reply(hook_ctx)
                    placeholder = hook_text or (
                        "(no reply — model returned an empty response. "
                        "Check gateway.log; if persistent, try "
                        "`agents.set_model` to swap to a stronger model.)"
                    )
                    dashboard_history.append({
                        "role": "assistant",
                        "content": placeholder,
                    })
                    dashboard_history.save()
                    yield SendParams(target=inbound.target, text=placeholder)
                    await self._hooks.run_on_turn_end(placeholder, hook_ctx)
                return
            # Only append the final assistant text message if it doesn't already
            # have tool_calls (i.e. if it was purely a text turn or the final
            # answer after tools). Avoid duplicating messages already appended
            # above in the tool-call walk.
            if not tool_uses_in_final:
                dashboard_history.append({"role": "assistant", "content": reply})
                dashboard_history.save()
            for chunk in chunk_text(reply, self._chunk_limit):
                yield SendParams(target=inbound.target, text=chunk)
            await self._hooks.run_on_turn_end(reply, hook_ctx)

    def _persist_usage(self, session_key: str) -> None:
        """Snapshot cumulative cache + cost telemetry for `usage.*` RPCs.

        Writes `<paths.usage_file(agent_id, session_key)>` with the
        observer's running totals; cost is computed from
        `model.pricing` (USD per million tokens) when present.
        """
        import json as _json

        sid = self._session_ids.get(session_key)
        if sid is None:
            return
        observer = self._observers.get(sid)
        if observer is None:
            return
        summary = observer.summary()
        cost_usd = 0.0
        pricing = getattr(self._model, "pricing", None) or {}
        for key, per_million in pricing.items():
            tokens = int(summary.get(_summary_key_for(key), 0))
            if tokens > 0:
                cost_usd += (tokens / 1_000_000) * float(per_million)
        path = self._paths.usage_file(self.id, session_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "turns": int(summary.get("turns", 0)),
            "input": int(summary.get("input", 0)),
            "output": int(summary.get("output", 0)),
            "cache_read": int(summary.get("cache_read", 0)),
            "cache_create": int(summary.get("cache_create", 0)),
            "hit_rate": float(summary.get("hit_rate", 0.0)),
            "cost_usd": round(cost_usd, 6),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(_json.dumps(payload), encoding="utf-8")
        import os as _os
        _os.replace(tmp, path)


def _summary_key_for(pricing_key: str) -> str:
    """Map a pricing-dict key (e.g. 'input_tokens') to the matching
    field on `CacheObserver.summary()` (e.g. 'input'). The pricing
    schema mirrors what providers report in their usage dicts."""
    aliases = {
        "input_tokens": "input",
        "output_tokens": "output",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_create",
    }
    return aliases.get(pricing_key, pricing_key)


__all__ = ["PiAgent"]
