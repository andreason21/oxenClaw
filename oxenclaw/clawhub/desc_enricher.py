"""LLM-driven description enricher for installed skills.

When a skill is installed via the clawhub installer, the model only
sees `manifest.description` — usually a one-liner — plus a SKILL.md
body excerpt in `<available_skills>`. That's enough for the model to
*read* the skill but not enough to *route* to it correctly: small
local models still call `web_search` for stock-analysis questions
because nothing in the description says "use this when the user asks
about stocks; don't use it for general web search".

This module asks the configured LLM to generate the missing routing
hints (WHEN TO USE / WHEN NOT TO USE / ALTERNATIVES) once at install
time, caches the result alongside the skill, and the loader merges
the cached hint into the rendered `<description>` block.

Design choices:

- **Cache-keyed by content hash.** We cache a sha256 of (name +
  description + body) so a re-install of the exact same skill is a
  no-op, but a content change forces a refresh on next install.
- **Same primary LLM as the agent.** No separate provider knob —
  whichever `Model` + `AuthStorage` the gateway uses for chat
  generates the hints too. Keeps the operator's surface small.
- **Best-effort.** If the call fails (no creds, offline, JSON
  malformed), we log + return None and the loader falls back to the
  raw description. Never break install over enrichment.
- **Triggered from installer only.** The loader is read-only; it
  never makes network calls. Adding a lazy "enrich on first read"
  path would slow `<available_skills>` rendering and surprise the
  operator with token spend.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.desc_enricher")

ENRICHED_FILE_NAME = "llm_desc.json"
ENV_DISABLE = "OXEN_SKILL_AUTO_ENRICH"  # set "0" / "false" to disable


class EnrichedDescription(BaseModel):
    """Routing hints derived from the SKILL.md by the primary LLM.

    The schema mirrors `tools_pkg._desc.hermes_desc` so the loader can
    render both tool-style and skill-style descriptions through the
    same template.
    """

    when_use: list[str] = Field(default_factory=list)
    when_skip: list[str] = Field(default_factory=list)
    alternatives: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class _EnrichmentRecord:
    """On-disk format for the cached enrichment."""

    content_hash: str
    enriched: EnrichedDescription
    model_id: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "content_hash": self.content_hash,
                "model_id": self.model_id,
                "enriched": self.enriched.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> _EnrichmentRecord | None:
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                return None
            enriched = EnrichedDescription.model_validate(data.get("enriched") or {})
            return cls(
                content_hash=str(data.get("content_hash") or ""),
                enriched=enriched,
                model_id=str(data.get("model_id") or ""),
            )
        except (json.JSONDecodeError, ValidationError, TypeError):
            return None


def content_hash(name: str, description: str, body: str) -> str:
    """Stable hash over the skill payload that drives the cache key."""
    h = hashlib.sha256()
    h.update(name.encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(description.encode("utf-8", errors="replace"))
    h.update(b"\x00")
    h.update(body.encode("utf-8", errors="replace"))
    return h.hexdigest()


def cache_path_for_skill(skill_dir: Path) -> Path:
    """Where the enrichment record lives for a given installed skill."""
    return skill_dir / ".clawhub" / ENRICHED_FILE_NAME


def load_cached(skill_dir: Path) -> _EnrichmentRecord | None:
    p = cache_path_for_skill(skill_dir)
    if not p.exists():
        return None
    try:
        return _EnrichmentRecord.from_json(p.read_text(encoding="utf-8"))
    except OSError:
        return None


def is_disabled() -> bool:
    """Operator kill-switch via env var."""
    raw = os.environ.get(ENV_DISABLE, "").strip().lower()
    return raw in {"0", "false", "no", "off"}


_PROMPT_TEMPLATE = """\
You classify when an oxenClaw skill should be invoked. Read the SKILL.md
below and emit STRICT JSON describing the routing hints. Be concrete
and brief.

Output schema:
{{
  "when_use": [<=4 short phrases starting "...">],
  "when_skip": [<=4 short phrases starting "..." about cases where the
                skill is the wrong tool],
  "alternatives": {{<other tool/skill name>: <one-line "use it when...">,
                    ...}}
}}

Rules:
- ≤80 chars per phrase. No markdown, no code fences.
- "when_use" reflects the skill's actual scripts and arg shapes.
- "when_skip" lists realistic confusions (e.g. "general web search —
  use web_search instead").
- "alternatives" names sibling oxenClaw tools (web_search, web_fetch,
  weather, github, skill_run, ...) only if relevant. Empty object is
  fine.
- Output ONLY the JSON object, no surrounding prose.

Skill name: {name}
Existing description: {description}

SKILL.md body (truncated):
---
{body}
---
"""

_BODY_CHARS_FOR_PROMPT = 4000


def _build_prompt(name: str, description: str, body: str) -> str:
    truncated = body if len(body) <= _BODY_CHARS_FOR_PROMPT else (
        body[:_BODY_CHARS_FOR_PROMPT] + "\n…(truncated)"
    )
    return _PROMPT_TEMPLATE.format(
        name=name,
        description=description.strip() or "(empty)",
        body=truncated.strip() or "(empty body)",
    )


def parse_llm_response(raw: str) -> EnrichedDescription | None:
    """Tolerate fenced code blocks / leading prose around the JSON object.

    Small models love to wrap their output in ```json fences or prefix
    with "Here is the JSON:" — strip those before parsing.
    """
    if not raw:
        return None
    text = raw.strip()
    # Strip outer fence if present.
    if text.startswith("```"):
        # remove the first fence line
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        # remove a trailing fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Find the first '{' and the matching last '}' — a cheap balance
    # walk avoids pulling in a full JSON parser for the lead-in.
    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    if end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    try:
        return EnrichedDescription.model_validate(data)
    except ValidationError:
        return None


async def enrich_skill_description(
    *,
    skill_dir: Path,
    name: str,
    description: str,
    body: str,
    model: Any,
    auth: Any,
) -> EnrichedDescription | None:
    """Generate + cache routing hints for a freshly installed skill.

    Returns the enriched description on success, None on any failure
    (kill-switch, network, malformed JSON). The caller's install path
    must NOT raise on a None result — enrichment is an optimisation.

    `model` and `auth` are the `Model` + `AuthStorage` the gateway is
    already using for chat. We re-use them directly so the operator
    doesn't configure a second provider just for this background call.
    """
    if is_disabled():
        logger.debug("desc enrichment disabled via %s", ENV_DISABLE)
        return None

    h = content_hash(name, description, body)
    existing = load_cached(skill_dir)
    if existing is not None and existing.content_hash == h:
        logger.debug("desc enrichment cache hit for %s", name)
        return existing.enriched

    # Imports deferred so a missing pi runtime doesn't block import.
    try:
        from oxenclaw.pi import (
            AssistantMessage,
            TextContent,
            UserMessage,
        )
        from oxenclaw.pi.auth import resolve_api
        from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
    except ImportError as exc:
        logger.warning("desc enrichment unavailable: %s", exc)
        return None

    prompt = _build_prompt(name, description, body)
    cfg = RuntimeConfig(temperature=0.0)
    try:
        api = await resolve_api(model, auth)
    except Exception as exc:
        logger.warning("desc enrichment skipped — could not resolve api: %s", exc)
        return None

    try:
        result = await run_agent_turn(
            model=model,
            api=api,
            system=(
                "You output a single strict JSON object describing skill "
                "routing hints. No prose, no fences, no commentary."
            ),
            history=[UserMessage(content=prompt)],
            tools=[],
            config=cfg,
        )
    except Exception as exc:
        logger.warning("desc enrichment LLM call failed for %s: %s", name, exc)
        return None

    final = result.final_message
    if not isinstance(final, AssistantMessage):
        return None
    text_blocks = [b.text for b in final.content if isinstance(b, TextContent)]
    raw = "\n".join(t for t in text_blocks if t)
    enriched = parse_llm_response(raw)
    if enriched is None:
        logger.warning(
            "desc enrichment for %s returned unparseable output (%d chars)",
            name,
            len(raw),
        )
        return None

    record = _EnrichmentRecord(
        content_hash=h,
        enriched=enriched,
        model_id=getattr(model, "id", "") or "",
    )
    try:
        cache_path = cache_path_for_skill(skill_dir)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(record.to_json(), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not persist enrichment cache for %s: %s", name, exc)
        # Still return the enriched value; caller can surface it.
    return enriched


def render_for_prompt(
    base_description: str,
    enriched: EnrichedDescription | None,
) -> str:
    """Compose the description string the loader injects into
    `<available_skills>`.

    Mirrors the hermes-style block produced by
    `tools_pkg._desc.hermes_desc`. Falls back to the raw description
    when no enrichment exists, so an offline / disabled gateway loses
    nothing.
    """
    parts: list[str] = [base_description.strip()]
    if enriched is None:
        return parts[0]
    if enriched.when_use:
        parts.append("WHEN TO USE: " + "; ".join(enriched.when_use) + ".")
    if enriched.when_skip:
        parts.append("WHEN NOT TO USE: " + "; ".join(enriched.when_skip) + ".")
    if enriched.alternatives:
        alt_lines = "; ".join(
            f"{name} ({why})" for name, why in enriched.alternatives.items()
        )
        parts.append("ALTERNATIVES: " + alt_lines + ".")
    return " ".join(parts)


__all__ = [
    "ENRICHED_FILE_NAME",
    "ENV_DISABLE",
    "EnrichedDescription",
    "cache_path_for_skill",
    "content_hash",
    "enrich_skill_description",
    "is_disabled",
    "load_cached",
    "parse_llm_response",
    "render_for_prompt",
]
