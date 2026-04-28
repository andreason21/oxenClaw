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
from pathlib import Path
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
    """Detailed XML recall block (with chunk_id + citation attrs).

    Stays at priority 80 (after the cacheable contributions) so the
    pi cache_control marker can still cover skills/embedded_context.
    The structural "agent doesn't remember" fix is the *prelude* —
    `format_memories_as_prelude` returns a tight plain-text bullet
    list that PiAgent prepends to the base prompt before assembly.
    That puts recall at the very top of the system prompt, where
    even small local models attend. The XML block here remains for
    large citation-aware models that benefit from chunk_id metadata.
    """
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


# ─── Ported from openclaw `buildAgentSystemPrompt` ───────────────────
#
# The next three helpers carry over openclaw's "## Skills (mandatory)",
# "## Memory Recall", and "## Execution Bias" sections almost verbatim.
# They are the *behavioural* part of openclaw's giant system prompt and
# are the most load-bearing in practice — instruction-following,
# tool-routing, and recall-attention quality all degrade noticeably
# without them on small local models. The wording is intentionally
# close to openclaw's so that operators comparing outputs across the
# two runtimes see consistent guidance.

EXECUTION_BIAS_BODY = (
    "## Execution Bias\n"
    "- Actionable request: act in this turn.\n"
    "- Non-final turn: use tools to advance, or ask for the one missing\n"
    "  decision that blocks safe progress.\n"
    "- Continue until done or genuinely blocked; do not finish with a\n"
    "  plan/promise when tools can move it forward.\n"
    "- Weak/empty tool result: vary query, path, command, or source\n"
    "  before concluding.\n"
    "- Mutable facts need live checks: files, git, clocks, versions,\n"
    "  services, processes, package state.\n"
    "- Final answer needs evidence: test/build/lint, screenshot,\n"
    "  inspection, tool output, or a named blocker."
)


def execution_bias_contribution() -> SystemPromptContribution:
    """Static "keep going / verify with tools" guidance.

    Mirrors openclaw `buildExecutionBiasSection`. Static text → cacheable.
    """
    return SystemPromptContribution(
        name="execution_bias",
        body=EXECUTION_BIAS_BODY,
        priority=15,
        cacheable=True,
    )


SKILLS_MANDATORY_BODY = (
    "## Skills (mandatory)\n"
    "Before replying: scan the `<available_skills>` `<description>`\n"
    "entries below.\n"
    "- If exactly one skill clearly applies: read its SKILL.md at\n"
    "  `<location>` (use the `read` / shell tool), then follow it.\n"
    "- If multiple could apply: choose the most specific one, then\n"
    "  read/follow it.\n"
    "- If none clearly apply: do not read any SKILL.md.\n"
    "Constraints: never read more than one skill up front; only read\n"
    "after selecting. Skills are documentation — never emit a tool_use\n"
    "block named after a skill (no such function is registered)."
)


def skills_mandatory_contribution() -> SystemPromptContribution:
    """Procedural guidance on how to consume `<available_skills>`.

    Sits at priority 18 so it lands directly above the XML skills block
    (priority 20) when both are present, mirroring openclaw's layout.
    """
    return SystemPromptContribution(
        name="skills_mandatory",
        body=SKILLS_MANDATORY_BODY,
        priority=18,
        cacheable=True,
    )


MEMORY_RECALL_BODY = (
    "## Memory Recall\n"
    "Before answering anything about prior work, decisions, dates,\n"
    "people, preferences, or todos: check the `<recalled_memories>`\n"
    "block below first; if it doesn't cover the question, call\n"
    "`memory_search(query=...)` and answer from the matching results.\n"
    "When you answer from a specific memory, cite it inline as\n"
    "`[mem:<id>]`. If recall is low-confidence after searching, say\n"
    "you checked rather than guessing."
)


def memory_recall_contribution() -> SystemPromptContribution:
    """Trigger conditions + citation rule for memory recall.

    Priority 70 keeps it above the XML `<recalled_memories>` block
    (priority 80) so the model reads the rule before the data.
    Cacheable (text is static; only the XML block changes per turn).
    """
    return SystemPromptContribution(
        name="memory_recall",
        body=MEMORY_RECALL_BODY,
        priority=70,
        cacheable=True,
    )


# ─── Project context auto-injection ──────────────────────────────────

# Canonical workspace files openclaw injects into every system prompt,
# in fixed display order. We mirror the same set + order so a project
# that already follows the openclaw convention (AGENTS.md, SOUL.md,
# etc.) is picked up unchanged when its directory is handed to PiAgent.
PROJECT_CONTEXT_FILES: tuple[str, ...] = (
    "AGENTS.md",
    "SOUL.md",
    "identity.md",
    "user.md",
    "tools.md",
    "bootstrap.md",
    "memory.md",
)

# Soft cap per file so a runaway AGENTS.md can't blow up the prompt.
# Matches the order-of-magnitude openclaw uses for stable context.
_PROJECT_CONTEXT_FILE_MAX_CHARS = 16_000


def load_project_context_files(
    project_dir: Path | str | None,
    *,
    filenames: tuple[str, ...] = PROJECT_CONTEXT_FILES,
    max_chars_per_file: int = _PROJECT_CONTEXT_FILE_MAX_CHARS,
) -> str:
    """Read canonical workspace files and render them as one Markdown block.

    Returns "" when no files are found so callers can blindly compose
    with `embedded_context_contribution(files_block=...)`. Filename
    lookup is case-insensitive but display preserves the canonical
    casing from `filenames` for stable cache prefixes.
    """
    if project_dir is None:
        return ""
    root = Path(project_dir)
    if not root.is_dir():
        return ""

    # Case-insensitive scan once → preserves canonical casing in output.
    actual_by_lower: dict[str, Path] = {}
    try:
        for entry in root.iterdir():
            if entry.is_file():
                actual_by_lower[entry.name.lower()] = entry
    except OSError:
        return ""

    sections: list[str] = []
    for canonical in filenames:
        path = actual_by_lower.get(canonical.lower())
        if path is None:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        content = content.strip()
        if not content:
            continue
        if len(content) > max_chars_per_file:
            content = content[:max_chars_per_file].rstrip() + "\n…[truncated]"
        sections.append(f"## {canonical}\n\n{content}")

    if not sections:
        return ""
    header = "# Project Context\nThe following project files have been loaded from the workspace."
    return header + "\n\n" + "\n\n".join(sections)


__all__ = [
    "PROJECT_CONTEXT_FILES",
    "PromptMode",
    "SystemPromptContribution",
    "assemble_system_prompt",
    "embedded_context_contribution",
    "execution_bias_contribution",
    "load_project_context_files",
    "memory_contribution",
    "memory_recall_contribution",
    "skills_contribution",
    "skills_mandatory_contribution",
    "time_contribution",
]
