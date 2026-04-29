"""Active memory — blocking memory sub-agent that runs BEFORE the main turn.

Mirrors openclaw `extensions/active-memory/index.ts`. The architectural
insight: small local models can't reliably pick out which retrieved
chunk answers a question. Instead, run a short LLM call with the
recent conversation + retrieved chunks, ask it to produce a one-line
"what does the user need from memory right now?" summary, and inject
THAT into the main turn's prompt.

The main model then sees a concise natural-language fact ("User lives
in Suwon; relevant for the weather question.") instead of 5 raw chunks
and has a much higher chance of using it.

Simpler than openclaw's full plugin (2K LOC) — drops the QMD search-
mode plumbing and the per-agent toggle UI. Same core flow:

  1. Resolve the active-memory model (defaults to the agent's main
     model — operators can override to a faster/cheaper one).
  2. Pull recent messages (config-bounded).
  3. Run hybrid recall on the latest user message.
  4. Build a short instruction prompt + send to the sub-model.
  5. Return the sub-model's reply text (or "" on timeout/error).

Callers integrate via `ActiveMemoryRunner.recall_for_turn(...)` and
prepend the result to the main system prompt or user message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from oxenclaw.memory.hybrid import HybridConfig
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.temporal_decay import TemporalDecayConfig
from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.models import Model
from oxenclaw.pi.run.runtime import RuntimeConfig
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.active")

# Phrases the sub-model emits when it decides nothing in memory is
# relevant. We treat them as "no recall context" and skip injection.
NO_RECALL_VALUES = frozenset(
    {
        "",
        "none",
        "no_reply",
        "no reply",
        "nothing useful",
        "nothing relevant",
        "no relevant memory",
        "no relevant memories",
        "n/a",
        "null",
        "[]",
        "{}",
    }
)


PromptStyle = Literal[
    "balanced",
    "strict",
    "contextual",
    "recall-heavy",
    "precision-heavy",
    "preference-only",
]


_PROMPT_BASE = (
    "You are the **active-memory sub-agent** for oxenClaw. The user just "
    "sent a new message. You have access to retrieved memory chunks. "
    "Your job: produce ONE short natural-language sentence (≤ 220 chars) "
    "that tells the main agent what the user actually wants from memory "
    "right now — using only facts that are clearly relevant.\n"
    "\n"
    "Rules:\n"
    "- If no chunk is clearly relevant, reply exactly: `none`.\n"
    "- Do NOT speculate. Only assert what the chunks say.\n"
    "- One sentence. No bullet lists. No citations. No commentary.\n"
    "- Use the user's language when possible.\n"
)


_PROMPT_STYLE_NUDGES: dict[PromptStyle, str] = {
    "balanced": "",
    "strict": "Bias HEAVILY towards `none` — only fire on direct fact lookups.",
    "contextual": "Include light contextual hints (preferences, ongoing tasks).",
    "recall-heavy": "Prefer to surface SOMETHING when a chunk plausibly applies.",
    "precision-heavy": "Only fire when a single chunk uniquely answers the question.",
    "preference-only": "Only return user preferences (location, name, role, etc).",
}


@dataclass
class ActiveMemoryConfig:
    """Operator knobs."""

    enabled: bool = True
    # Halved from the original 8s after production logs showed every
    # chat turn paying ~8s of dead time when the local sub-model
    # couldn't return a one-line summary in time. 4s is still enough
    # for any model that can reasonably back this feature; slow models
    # gracefully degrade to "no recall summary" instead of stalling
    # the user-visible reply.
    timeout_seconds: float = 4.0
    max_summary_chars: int = 220
    recent_user_turns: int = 2
    recent_assistant_turns: int = 1
    recent_user_chars: int = 220
    recent_assistant_chars: int = 180
    prompt_style: PromptStyle = "balanced"
    prompt_override: str | None = None
    prompt_append: str | None = None
    top_k: int = 5
    # When None, ActiveMemoryRunner reuses the main agent's model.
    model_id: str | None = None
    cache_ttl_seconds: float = 15.0
    logging: bool = True


@dataclass
class _CacheEntry:
    summary: str
    expires_at: float


@dataclass
class ActiveMemoryRunner:
    """Per-agent active-memory orchestrator."""

    memory: MemoryRetriever
    main_model: Model
    api_resolver: Any  # async (Model) → Api
    config: ActiveMemoryConfig = field(default_factory=ActiveMemoryConfig)
    # Optional override Model object (when config.model_id resolves).
    sub_model: Model | None = None

    _cache: dict[str, _CacheEntry] = field(default_factory=dict)

    async def recall_for_turn(
        self,
        *,
        query: str,
        recent_messages: list[Any] | None = None,
        session_key: str | None = None,
    ) -> str:
        """Return the sub-agent's one-line memory summary, or "" when
        the sub-agent declined OR timed out.

        Caller is responsible for embedding the result in the main
        prompt — typically as a system-prompt prelude line:

            ## Active memory recall
            User lives in Suwon, South Korea — relevant to the weather question.

        The main turn then attends to this rather than raw chunks.
        """
        if not self.config.enabled or not query.strip():
            return ""
        # 15s TTL cache keyed by (session, query). Same query inside the
        # window gets the same answer — the user's intent rarely shifts
        # in 15 seconds.
        cache_key = f"{session_key or '_'}:{query[:200]}"
        now = asyncio.get_event_loop().time()
        cached = self._cache.get(cache_key)
        if cached and cached.expires_at > now:
            if self.config.logging:
                logger.info("active-memory cache hit: query=%r", query[:80])
            return cached.summary
        try:
            summary = await asyncio.wait_for(
                self._recall_inner(query, recent_messages or []),
                timeout=self.config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "active-memory timeout after %.1fs: query=%r",
                self.config.timeout_seconds,
                query[:80],
            )
            return ""
        except Exception:
            logger.exception("active-memory failed: query=%r", query[:80])
            return ""
        # Sanitize sub-model "none" replies.
        normalised = (summary or "").strip()
        if normalised.lower() in NO_RECALL_VALUES:
            normalised = ""
        if len(normalised) > self.config.max_summary_chars:
            normalised = normalised[: self.config.max_summary_chars].rstrip() + "…"
        self._cache[cache_key] = _CacheEntry(
            summary=normalised,
            expires_at=now + self.config.cache_ttl_seconds,
        )
        if self.config.logging:
            preview = normalised.replace("\n", " ")[:160] or "(none)"
            logger.info("active-memory: query=%r → summary=%r", query[:80], preview)
        return normalised

    async def _recall_inner(self, query: str, recent_messages: list[Any]) -> str:
        # Hybrid + decay search — same as the main turn's auto-recall.
        hits = await self.memory.search(
            query=query,
            k=self.config.top_k,
            hybrid=HybridConfig(enabled=True),
            temporal_decay=TemporalDecayConfig(enabled=True),
        )
        if not hits:
            return ""
        chunks_block = "\n\n".join(
            f"[chunk {i + 1} score={h.score:.3f} cite={h.citation}]\n{h.chunk.text.strip()[:600]}"
            for i, h in enumerate(hits)
        )
        recent_block = self._render_recent(recent_messages)
        prompt_body = self._build_prompt(query=query, chunks=chunks_block, recent=recent_block)

        # Run the sub-agent inference. We use the cheapest path:
        # `run_attempt` directly with no tools and a tight max_tokens.
        from oxenclaw.pi.run.attempt import run_attempt

        sub_model = self.sub_model or self.main_model
        api = await self.api_resolver(sub_model)
        result = await run_attempt(
            model=sub_model,
            api=api,
            system=prompt_body,
            messages=[UserMessage(content="Produce the one-sentence summary now.")],
            tools=[],
            config=RuntimeConfig(
                temperature=0.0,
                max_tokens=160,
                timeout_seconds=self.config.timeout_seconds,
                preemptive_compaction=False,
                stop_reason_recovery_attempts=0,
            ),
        )
        if not isinstance(result.message, AssistantMessage):
            return ""
        text = "".join(b.text for b in result.message.content if isinstance(b, TextContent)).strip()
        return text

    def _render_recent(self, messages: list[Any]) -> str:
        """Pull the last few turns, bounded by config."""
        if not messages:
            return ""
        users: list[str] = []
        assts: list[str] = []
        # Walk from end backwards.
        for msg in reversed(messages):
            if isinstance(msg, UserMessage):
                if len(users) < self.config.recent_user_turns:
                    text = msg.content if isinstance(msg.content, str) else "(media)"
                    if len(text) > self.config.recent_user_chars:
                        text = text[: self.config.recent_user_chars] + "…"
                    users.append(text)
            elif isinstance(msg, AssistantMessage):
                if len(assts) < self.config.recent_assistant_turns:
                    text = "".join(b.text for b in msg.content if isinstance(b, TextContent))
                    if len(text) > self.config.recent_assistant_chars:
                        text = text[: self.config.recent_assistant_chars] + "…"
                    assts.append(text)
            if (
                len(users) >= self.config.recent_user_turns
                and len(assts) >= self.config.recent_assistant_turns
            ):
                break
        # Re-order to natural chronological.
        users.reverse()
        assts.reverse()
        lines: list[str] = []
        for u, a in zip(users, assts + [""] * len(users), strict=False):
            if u:
                lines.append(f"USER: {u}")
            if a:
                lines.append(f"ASSISTANT: {a}")
        return "\n".join(lines)

    def _build_prompt(self, *, query: str, chunks: str, recent: str) -> str:
        if self.config.prompt_override:
            base = self.config.prompt_override
        else:
            base = _PROMPT_BASE
            nudge = _PROMPT_STYLE_NUDGES.get(self.config.prompt_style, "")
            if nudge:
                base = base + "\n" + nudge
        if self.config.prompt_append:
            base = base + "\n" + self.config.prompt_append
        return (
            f"{base}\n\n"
            f"## Recent conversation\n{recent or '(empty)'}\n\n"
            f"## Retrieved memory chunks\n{chunks or '(none)'}\n\n"
            f"## User's latest message\n{query}"
        )


def format_active_memory_prelude(summary: str) -> str:
    """Render the sub-agent summary as a prompt prelude.

    The main agent treats this as ground-truth context. Empty / None
    short-circuits to "" so callers can blindly compose with f-strings.
    """
    if not summary or not summary.strip():
        return ""
    return (
        "## Active memory recall\n"
        "The following ONE fact has been pre-distilled from your "
        "long-term memory by a sub-agent and is relevant to the user's "
        "current message. Use it directly when answering:\n\n"
        f"{summary.strip()}"
    )


__all__ = [
    "NO_RECALL_VALUES",
    "ActiveMemoryConfig",
    "ActiveMemoryRunner",
    "PromptStyle",
    "format_active_memory_prelude",
]
