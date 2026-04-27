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
from typing import Any

import oxenclaw.pi.providers  # noqa: F401  registers stream wrappers
from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.history import ConversationHistory
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
    ToolUseBlock,
    UserMessage,
    default_registry,
    resolve_api,
)
from oxenclaw.pi.cache_observability import CacheObserver, should_apply_cache_markers
from oxenclaw.pi.compaction import truncating_summarizer
from oxenclaw.pi.context_engine import (
    ContextEngine,
    ContextEngineRuntimeContext,
    LegacyContextEngine,
)
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
    "  - `memory_save(text, tags?)` — append a stable fact to the\n"
    "    inbox whenever the user asks you to remember something OR you\n"
    "    learn a durable preference (their name, role, deadline,\n"
    "    tooling preference). Skip ephemeral chat-transcript details.\n"
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
        include_skills: bool = True,
        chunk_limit: int = 4_000,
        thinking: ThinkingLevel | str | None = None,
        compact_keep_tail_turns: int = 6,
        context_engine: ContextEngine | None = None,
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

        # Context engine — defaults to the pass-through `LegacyContextEngine`
        # so behaviour matches pre-rc.16 PiAgent byte-for-byte. Operators
        # opt into a custom strategy by injecting it here.
        self._engine: ContextEngine = context_engine or LegacyContextEngine()

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

        # Parallel ConversationHistory write so the dashboard's `chat.history`
        # poll surfaces this turn. PiAgent's pi-format SessionManager and the
        # dashboard-format ConversationHistory are intentionally separate
        # stores — pi owns the rich transcript the runner needs, while the
        # dashboard wants a flat role/content list. We write the user turn
        # eagerly here and the assistant turn after the inference loop.
        dashboard_history = ConversationHistory(
            self._paths.session_file(self.id, ctx.session_key)
        )
        if isinstance(user_content, str):
            dashboard_history.append({"role": "user", "content": user_content})
        else:
            text_only = "\n".join(
                b.text for b in user_content if isinstance(b, TextContent)
            )
            dashboard_history.append({"role": "user", "content": text_only or "(image)"})
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

        ts_turn_start = time.time()
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
        await self._engine.compact(
            session_id=session.id,
            messages=session.messages,
            token_budget=self._model.context_window,
            runtime_context=ContextEngineRuntimeContext(
                extra={
                    "session": session,
                    "summarizer": truncating_summarizer,
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
            if not reply:
                return
            # Only append the final assistant text message if it doesn't already
            # have tool_calls (i.e. if it was purely a text turn or the final
            # answer after tools). Avoid duplicating messages already appended
            # above in the tool-call walk.
            tool_uses_in_final = [
                b for b in result.final_message.content if isinstance(b, ToolUseBlock)
            ]
            if not tool_uses_in_final:
                dashboard_history.append({"role": "assistant", "content": reply})
                dashboard_history.save()
            for chunk in chunk_text(reply, self._chunk_limit):
                yield SendParams(target=inbound.target, text=chunk)

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
