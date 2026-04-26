"""System-prompt assembly.

Mirrors `pi-embedded-runner/system-prompt.ts` + the underlying
`buildAgentSystemPrompt`. The contract: the runtime hands the assembler
a base prompt + a list of *contributions* (skills, memory recall,
embedded context files, time block, prompt-mode header), and the
assembler concatenates them in a stable order separated by blank lines.

Each `SystemPromptContribution` has a `priority` (lower goes first) and
optional `cacheable` flag. The Anthropic cache_control marker is placed
after the last cacheable contribution; non-cacheable trailing content
(e.g. dynamic memory recall) sits below the marker so it doesn't
invalidate the cache prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PromptMode = Literal["chat", "code", "qa", "structured", "tool"]


@dataclass(frozen=True)
class SystemPromptContribution:
    """One slice that goes into the assembled system prompt."""

    name: str
    body: str
    priority: int = 100
    cacheable: bool = True
    # When False, runtime omits this contribution (e.g. an empty memory recall).
    enabled: bool = True


def assemble_system_prompt(
    base: str,
    contributions: list[SystemPromptContribution],
    *,
    mode: PromptMode = "chat",
) -> tuple[str, int]:
    """Return `(prompt, cacheable_prefix_length)`.

    Contributions are sorted by `priority` (stable). The split point between
    the cacheable prefix and the volatile suffix is the index just after
    the last enabled cacheable contribution; the runtime can use this to
    place an Anthropic cache_control marker.

    `mode` is appended as a tiny footer hint that downstream provider
    adapters can use to pick generation defaults.
    """
    enabled = [c for c in contributions if c.enabled and c.body.strip()]
    enabled_sorted = sorted(enabled, key=lambda c: c.priority)
    parts: list[str] = []
    if base.strip():
        parts.append(base.strip())
    cache_prefix_end = -1
    for idx, c in enumerate(enabled_sorted):
        parts.append(c.body.strip())
        if c.cacheable:
            cache_prefix_end = idx
    if mode and mode != "chat":
        parts.append(f"[mode:{mode}]")
    prompt = "\n\n".join(parts)
    return prompt, cache_prefix_end + 1  # 1-based "first N parts"


# ─── Common contributions ────────────────────────────────────────────


def time_contribution(*, iso_now: str, timezone: str) -> SystemPromptContribution:
    """Embed the current time / timezone so the model doesn't hallucinate."""
    return SystemPromptContribution(
        name="time",
        body=f"Current time: {iso_now} ({timezone})",
        priority=10,
        cacheable=False,  # changes every turn → must not be in cache prefix
    )


def skills_contribution(*, skills_block: str) -> SystemPromptContribution:
    return SystemPromptContribution(
        name="skills",
        body=skills_block,
        priority=20,
        cacheable=True,
    )


def memory_contribution(*, memory_block: str) -> SystemPromptContribution:
    return SystemPromptContribution(
        name="memory",
        body=memory_block,
        priority=80,
        cacheable=False,  # recall query-dependent → not cacheable
    )


def embedded_context_contribution(*, files_block: str) -> SystemPromptContribution:
    return SystemPromptContribution(
        name="embedded_context",
        body=files_block,
        priority=30,
        cacheable=True,
    )


__all__ = [
    "PromptMode",
    "SystemPromptContribution",
    "assemble_system_prompt",
    "embedded_context_contribution",
    "memory_contribution",
    "skills_contribution",
    "time_contribution",
]
