"""Per-turn LLM-based fact extractor.

Extends `oxenclaw.memory.dreaming` (which runs on a whole session at
night) to a 1-turn window that fires after every user message and
catches durable facts the regex backstop in
`oxenclaw.memory.auto_extract` can't pattern-match — preferences,
deadlines, project context, decisions phrased in free form.

Why both regex AND LLM?
- Regex (auto_extract): fires on every turn, ~µs latency, 100%
  reliable on the small set of shapes it knows. Catches the simplest
  cases ("X는 우리 형이야", "I live in Y").
- LLM (this module): fires when enabled, 1–3s latency, catches
  open-ended facts. Optional — operators with weak local models or
  latency budgets can leave it off.

Fire-and-forget — the caller schedules the coroutine via
`asyncio.create_task` so the user's response never waits on the
extraction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.memory.dreaming import _parse_facts_json
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.pi import AssistantMessage, TextContent, UserMessage
from oxenclaw.pi.models import Model
from oxenclaw.pi.run import RuntimeConfig
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.turn_dream")


_TURN_PROMPT = (
    "You are extracting durable facts from a SINGLE user message. "
    "Output JSON only — one object with key `facts` whose value is "
    "an array. Each fact has `text` (a complete natural-language "
    "sentence ≤ 240 chars), `confidence` (0.0..1.0), and `tags` "
    "(short labels like `family`, `location`, `preference`, "
    "`decision`, `deadline`, `name`, `role`).\n"
    "\n"
    "Rules:\n"
    "- Only DURABLE facts worth remembering across future sessions.\n"
    "- Skip questions, greetings, ephemeral chatter, tool requests.\n"
    "- Confidence ≥ 0.7 only when the user stated it directly. ≤ 0.5 otherwise.\n"
    "- Use the user's language for `text`. If Korean, also include "
    "  an English paraphrase in the same `text` so cross-lingual "
    "  recall hits both ways.\n"
    "- Empty array is fine — most messages have no durable fact.\n"
    "\n"
    "Output exactly:\n"
    '{"facts": [{"text": "...", "confidence": 0.8, "tags": ["..."]}]}'
)


@dataclass
class TurnDreamConfig:
    """Operator knobs for the per-turn dreamer.

    Default off — adds an LLM call per user message. Enable with
    `OXENCLAW_TURN_DREAM=1` once you've confirmed your local model
    can absorb the latency.
    """

    enabled: bool = False
    timeout_seconds: float = 8.0
    # Skip messages shorter than this (after .strip()). Single-word
    # replies and emoji are almost never durable.
    min_chars: int = 8
    max_facts_per_turn: int = 5
    inbox_tag: str = "turn-dream"
    # Use the main model when None.
    model_id: str | None = None
    # Drop facts with confidence below this threshold even if the
    # model emitted them.
    min_confidence: float = 0.5
    logging: bool = True


@dataclass
class TurnDreamResult:
    saved_facts: list[str] = field(default_factory=list)
    error: str | None = None
    skipped_reason: str | None = None


async def dream_turn(
    *,
    user_text: str,
    memory: MemoryRetriever,
    sub_model: Model,
    api_resolver: Any,
    config: TurnDreamConfig | None = None,
    already_saved: list[str] | None = None,
) -> TurnDreamResult:
    """Extract durable facts from one user message and save them.

    Idempotent at the inbox level — duplicate calls append duplicate
    rows but the hybrid+decay search dedupes recall hits, so the
    cost of a duplicate is just disk space.

    `already_saved` lets the caller pass the regex backstop's output
    so the LLM doesn't get rewarded for re-emitting facts the
    deterministic layer already covered. Comparison is exact-string;
    we don't try to be clever about near-duplicates here (recall
    layer handles that).
    """
    cfg = config or TurnDreamConfig()
    result = TurnDreamResult()
    if not cfg.enabled:
        result.skipped_reason = "turn-dream disabled"
        return result
    text = (user_text or "").strip()
    if len(text) < cfg.min_chars:
        result.skipped_reason = f"text too short ({len(text)} < {cfg.min_chars})"
        return result

    try:
        from oxenclaw.pi.run.attempt import run_attempt

        api = await api_resolver(sub_model)
        attempt = await asyncio.wait_for(
            run_attempt(
                model=sub_model,
                api=api,
                system=_TURN_PROMPT,
                messages=[UserMessage(content=f"User message:\n{text}")],
                tools=[],
                config=RuntimeConfig(
                    temperature=0.0,
                    max_tokens=512,
                    timeout_seconds=cfg.timeout_seconds,
                    preemptive_compaction=False,
                    stop_reason_recovery_attempts=0,
                ),
            ),
            timeout=cfg.timeout_seconds,
        )
    except TimeoutError:
        result.error = f"timeout after {cfg.timeout_seconds:.1f}s"
        if cfg.logging:
            logger.warning("turn-dream %s: text=%r", result.error, text[:80])
        return result
    except Exception as exc:
        result.error = f"run_attempt failed: {exc}"
        logger.exception("turn-dream failed")
        return result

    if not isinstance(attempt.message, AssistantMessage):
        result.error = "no assistant message"
        return result
    raw_text = "".join(
        b.text for b in attempt.message.content if isinstance(b, TextContent)
    ).strip()
    if not raw_text:
        result.skipped_reason = "empty model output"
        return result

    facts = _parse_facts_json(raw_text)
    if not facts:
        result.skipped_reason = "no durable facts extracted"
        return result

    facts = facts[: cfg.max_facts_per_turn]
    already = set(already_saved or [])

    for fact in facts:
        ftext = (fact.get("text") or "").strip()
        if not ftext:
            continue
        try:
            confidence = float(fact.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        if confidence < cfg.min_confidence:
            continue
        if ftext in already:
            continue
        tags = fact.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        all_tags = sorted({*[str(t) for t in tags if isinstance(t, str)], cfg.inbox_tag})
        try:
            await memory.save(ftext, tags=all_tags)
        except Exception:
            logger.exception("turn-dream inbox save failed: text=%r", ftext[:80])
            continue
        result.saved_facts.append(ftext)

    if cfg.logging and result.saved_facts:
        logger.info("turn-dream: saved %d fact(s) for text=%r", len(result.saved_facts), text[:80])
    return result


__all__ = ["TurnDreamConfig", "TurnDreamResult", "dream_turn"]
