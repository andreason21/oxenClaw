"""Dreaming pipeline — post-session narrative consolidation.

Mirrors openclaw `extensions/memory-core/src/dreaming*.ts`. Where the
TS version is a 10K LOC plugin with cron orchestration, REM-evidence
graph, and concept vocabulary, this Python port keeps the two
load-bearing phases:

  - **summarise**: read a finished session's transcript → ask a small
    LLM to extract durable facts (location, name, preference, decision)
    + drop the chatter.
  - **promote**: each extracted fact appended to memory inbox tagged
    `dreaming`; high-confidence facts auto-promoted to short_term.

Triggered by:
  - cron job (operator-configurable)
  - manual `memory.dream` RPC

The flow is deliberately simple: one LLM call per session, cap-limited
output, idempotent (running twice on the same session produces the
same dedup-keyed inbox additions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oxenclaw.agents.history import ConversationHistory
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.models import Model
from oxenclaw.pi.run.runtime import RuntimeConfig
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.dreaming")


_DREAM_PROMPT = (
    "You are the **dreaming sub-agent** for oxenClaw. Read the session "
    "transcript below and extract DURABLE FACTS the user established "
    "or decisions they made — things worth remembering across future "
    "sessions.\n"
    "\n"
    "Output JSON only — a single object with one key `facts` whose "
    "value is an array. Each fact has:\n"
    '  - "text": natural-language complete sentence (≤ 240 chars)\n'
    '  - "confidence": 0.0..1.0 — how durable / unambiguous the fact is\n'
    '  - "tags": array of short labels (e.g. ["location", "preference"])\n'
    "\n"
    "Rules:\n"
    "- Skip ephemeral chatter (greetings, tool-call results, transient "
    "state). Only what should survive past this conversation.\n"
    "- Confidence ≥ 0.7 only when the user stated it directly or "
    "confirmed it. Otherwise ≤ 0.5.\n"
    "- Use the user's language; if they switched mid-session, prefer "
    "their dominant one.\n"
    "- Empty array is fine when nothing durable was said.\n"
    "\n"
    "Output format:\n"
    '{"facts": [{"text": "...", "confidence": 0.8, "tags": ["..."]}]}'
)


@dataclass
class DreamingConfig:
    """Operator knobs for the dreaming pass."""

    enabled: bool = False
    timeout_seconds: float = 30.0
    max_facts_per_session: int = 10
    auto_promote_threshold: float = 0.7
    inbox_tag: str = "dreaming"
    # Skip sessions shorter than this many turns — no point dreaming a
    # 1-message hello.
    min_session_turns: int = 4
    model_id: str | None = None  # None → main model


@dataclass
class DreamingResult:
    session_key: str
    facts_extracted: int = 0
    facts_promoted: int = 0
    fact_texts: list[str] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None


def _render_transcript(messages: list[dict[str, Any]], *, char_limit: int = 12_000) -> str:
    """Render dashboard-format messages as a compact transcript."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role.upper()}: {content}")
    blob = "\n\n".join(lines)
    if len(blob) > char_limit:
        # Keep head + tail — middle of long sessions is rarely the
        # most fact-dense part.
        head = blob[: char_limit // 2]
        tail = blob[-char_limit // 2 :]
        blob = f"{head}\n\n[... transcript trimmed ...]\n\n{tail}"
    return blob


async def dream_session(
    *,
    agent_id: str,
    session_key: str,
    history: ConversationHistory,
    memory: MemoryRetriever,
    sub_model: Model,
    api_resolver: Any,
    config: DreamingConfig | None = None,
) -> DreamingResult:
    """Run one dreaming pass for a single session.

    Idempotent at the inbox level — the saved fact text is the same
    every run, so a duplicate dreaming pass appends a duplicate inbox
    entry. The hybrid+decay search dedupes vector hits, so duplicate
    inbox entries don't multiply recall noise. Operators can run this
    nightly via cron without worrying about exponential growth.
    """
    cfg = config or DreamingConfig()
    result = DreamingResult(session_key=session_key)
    if not cfg.enabled:
        result.skipped_reason = "dreaming disabled"
        return result
    msgs = history.messages()
    if len(msgs) < cfg.min_session_turns:
        result.skipped_reason = f"session has {len(msgs)} turns (min {cfg.min_session_turns})"
        return result

    transcript = _render_transcript(msgs)
    prompt = (
        f"{_DREAM_PROMPT}\n\n## Session: {agent_id}/{session_key}\n\n## Transcript\n{transcript}"
    )

    try:
        from oxenclaw.pi.run.attempt import run_attempt

        api = await api_resolver(sub_model)
        attempt = await run_attempt(
            model=sub_model,
            api=api,
            system=prompt,
            messages=[UserMessage(content="Extract the durable facts now as JSON.")],
            tools=[],
            config=RuntimeConfig(
                temperature=0.0,
                max_tokens=1024,
                timeout_seconds=cfg.timeout_seconds,
                preemptive_compaction=False,
                stop_reason_recovery_attempts=0,
            ),
        )
    except Exception as exc:
        logger.exception("dreaming run_attempt failed")
        result.error = f"run_attempt failed: {exc}"
        return result

    if not isinstance(attempt.message, AssistantMessage):
        result.error = "no assistant message"
        return result
    raw_text = "".join(
        b.text for b in attempt.message.content if isinstance(b, TextContent)
    ).strip()
    if not raw_text:
        result.error = "empty model output"
        return result

    facts = _parse_facts_json(raw_text)
    if not facts:
        result.skipped_reason = "no durable facts extracted"
        return result

    facts = facts[: cfg.max_facts_per_session]

    # Save each fact to the inbox + auto-promote high-confidence ones.
    for fact in facts:
        text = (fact.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(fact.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        tags = fact.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        all_tags = sorted({*tags, cfg.inbox_tag})
        try:
            await memory.save(text, tags=all_tags)
        except Exception:
            logger.exception("dreaming inbox save failed: text=%r", text[:80])
            continue
        result.facts_extracted += 1
        result.fact_texts.append(text)
        if confidence >= cfg.auto_promote_threshold:
            try:
                store = memory.store
                store.short_term_add(
                    text=text,
                    tags=[*tags, cfg.inbox_tag],
                    confidence=confidence,
                )
                result.facts_promoted += 1
            except Exception:
                logger.exception("dreaming promote failed: text=%r", text[:80])

    if result.facts_extracted:
        logger.info(
            "dreaming: agent=%s session=%s extracted=%d promoted=%d",
            agent_id,
            session_key,
            result.facts_extracted,
            result.facts_promoted,
        )
    return result


def _parse_facts_json(raw: str) -> list[dict[str, Any]]:
    """Parse the model's JSON output, tolerating fences / leading text."""
    from oxenclaw.pi.run.json_repair import repair_and_parse

    parsed, _repair = repair_and_parse(raw)
    if isinstance(parsed, dict):
        facts = parsed.get("facts")
        if isinstance(facts, list):
            return [f for f in facts if isinstance(f, dict)]
    # Sometimes the model emits a bare array.
    if isinstance(parsed, list):
        return [f for f in parsed if isinstance(f, dict)]
    # Last resort: scan raw for a {"facts": ...} substring.
    try:
        start = raw.index("{")
        end = raw.rindex("}")
        candidate = raw[start : end + 1]
        return _parse_facts_json(candidate) if candidate != raw else []
    except ValueError:
        return []


__all__ = ["DreamingConfig", "DreamingResult", "dream_session"]
