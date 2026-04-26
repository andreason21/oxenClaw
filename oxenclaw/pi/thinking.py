"""ThinkingLevel — Anthropic / Gemini extended-reasoning budget knob.

Mirrors `@mariozechner/pi-agent-core` `ThinkingLevel`. Five steps from
"off" to "ultra"; each provider adapter maps to its native budget:

- Anthropic: `thinking.budget_tokens` (1024 → 32_000).
- Gemini:    `thinking_config.thinking_budget` (256 → 24_576).
- OpenAI o1/o3: `reasoning_effort` (low/medium/high — collapses 5→3).

Run loop converts level → provider-specific param at attempt-build time.
"""

from __future__ import annotations

from enum import StrEnum


class ThinkingLevel(StrEnum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    ULTRA = "ultra"


# Per-level token budgets used by Anthropic-family providers.
ANTHROPIC_THINKING_BUDGETS: dict[ThinkingLevel, int] = {
    ThinkingLevel.OFF: 0,
    ThinkingLevel.LOW: 1_024,
    ThinkingLevel.MEDIUM: 4_096,
    ThinkingLevel.HIGH: 16_000,
    ThinkingLevel.ULTRA: 32_000,
}


# OpenAI o-series accepts `reasoning_effort: low/medium/high` only; map
# our 5-step scale onto its 3-step.
OPENAI_REASONING_EFFORT: dict[ThinkingLevel, str | None] = {
    ThinkingLevel.OFF: None,
    ThinkingLevel.LOW: "low",
    ThinkingLevel.MEDIUM: "medium",
    ThinkingLevel.HIGH: "high",
    ThinkingLevel.ULTRA: "high",
}


GEMINI_THINKING_BUDGETS: dict[ThinkingLevel, int] = {
    ThinkingLevel.OFF: 0,
    ThinkingLevel.LOW: 256,
    ThinkingLevel.MEDIUM: 2_048,
    ThinkingLevel.HIGH: 8_192,
    ThinkingLevel.ULTRA: 24_576,
}


__all__ = [
    "ANTHROPIC_THINKING_BUDGETS",
    "GEMINI_THINKING_BUDGETS",
    "OPENAI_REASONING_EFFORT",
    "ThinkingLevel",
]
