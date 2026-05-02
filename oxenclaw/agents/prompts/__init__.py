"""System-prompt builder — section catalog + composer.

Splits the monolithic agent system prompt into named, conditionally-
injected sections. Compared to a single static string this:

- skips sections whose tool isn't loaded (smaller context for trimmed
  deployments),
- adds model-family overlays for non-thinking small models that need
  explicit tool-use enforcement (qwen / llama / gemma / mistral),
- adds channel-specific markdown / media hints,
- caches the assembled prompt on the agent so we only rebuild after
  context compaction (mirrors hermes-agent / openclaw cache discipline).

Entry point: :func:`build_system_prompt`.
"""

from oxenclaw.agents.prompts.builder import (
    ANTI_REFUSAL,
    DEFAULT_IDENTITY,
    MEMORY_GUIDANCE,
    PLATFORM_HINTS,
    SKILLS_GUIDANCE,
    TIME_GUIDANCE,
    TOOL_CALL_BASIC,
    TOOL_USE_ENFORCEMENT,
    TOOL_USE_ENFORCEMENT_MODELS,
    WEATHER_PLAYBOOK,
    WEB_RESEARCH_PLAYBOOK,
    WIKI_PLAYBOOK,
    build_system_prompt,
    needs_tool_use_enforcement,
)

__all__ = [
    "ANTI_REFUSAL",
    "DEFAULT_IDENTITY",
    "MEMORY_GUIDANCE",
    "PLATFORM_HINTS",
    "SKILLS_GUIDANCE",
    "TIME_GUIDANCE",
    "TOOL_CALL_BASIC",
    "TOOL_USE_ENFORCEMENT",
    "TOOL_USE_ENFORCEMENT_MODELS",
    "WEATHER_PLAYBOOK",
    "WEB_RESEARCH_PLAYBOOK",
    "WIKI_PLAYBOOK",
    "build_system_prompt",
    "needs_tool_use_enforcement",
]
