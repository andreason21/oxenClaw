"""Section catalog + composer for the agent system prompt."""

from __future__ import annotations

from oxenclaw.agents.prompts import (
    ANTI_REFUSAL,
    DEFAULT_IDENTITY,
    MEMORY_GUIDANCE,
    PLATFORM_HINTS,
    SKILLS_GUIDANCE,
    TIME_GUIDANCE,
    TOOL_CALL_BASIC,
    TOOL_USE_ENFORCEMENT,
    WEATHER_PLAYBOOK,
    WEB_RESEARCH_PLAYBOOK,
    WIKI_PLAYBOOK,
    build_system_prompt,
    needs_tool_use_enforcement,
)


# ── needs_tool_use_enforcement ───────────────────────────────────────


def test_enforcement_yes_for_small_local_models() -> None:
    assert needs_tool_use_enforcement("qwen3.5:9b") is True
    assert needs_tool_use_enforcement("qwen2.5-coder:7b") is True
    assert needs_tool_use_enforcement("llama3.1:8b") is True
    assert needs_tool_use_enforcement("gemma4:latest") is True
    assert needs_tool_use_enforcement("mistral:7b-instruct") is True


def test_enforcement_no_for_frontier_models() -> None:
    assert needs_tool_use_enforcement("claude-sonnet-4-6") is False
    assert needs_tool_use_enforcement("claude-opus-4-7") is False
    assert needs_tool_use_enforcement("gpt-5") is False
    assert needs_tool_use_enforcement("gpt-4o-mini") is False
    assert needs_tool_use_enforcement("gemini-2.0-flash") is False
    assert needs_tool_use_enforcement("gemini-2.5-pro") is False


def test_enforcement_no_for_thinking_variants() -> None:
    """Thinking variants of small models reason internally and don't
    benefit from the enforcement overlay."""
    assert needs_tool_use_enforcement("qwen3-thinking:32b") is False
    assert needs_tool_use_enforcement("qwq:32b") is False
    assert needs_tool_use_enforcement("deepseek-r1:32b") is False


def test_enforcement_no_for_empty_or_unknown() -> None:
    assert needs_tool_use_enforcement("") is False
    assert needs_tool_use_enforcement("custom-finetune-v3") is False


# ── build_system_prompt section toggling ─────────────────────────────


def test_minimal_prompt_has_only_always_on_sections() -> None:
    """No tools loaded → identity + tool-call basic + time only."""
    p = build_system_prompt(model_id="claude-sonnet-4-6", tool_names=())
    assert DEFAULT_IDENTITY in p
    assert TOOL_CALL_BASIC in p
    assert TIME_GUIDANCE in p
    # All conditional sections must be absent.
    assert MEMORY_GUIDANCE not in p
    assert SKILLS_GUIDANCE not in p
    assert ANTI_REFUSAL not in p
    assert WEATHER_PLAYBOOK not in p
    assert WEB_RESEARCH_PLAYBOOK not in p
    assert WIKI_PLAYBOOK not in p


def test_memory_section_only_when_memory_save_loaded() -> None:
    p_off = build_system_prompt(model_id="claude-sonnet-4-6", tool_names=())
    p_on = build_system_prompt(
        model_id="claude-sonnet-4-6", tool_names=("memory_save",)
    )
    assert MEMORY_GUIDANCE not in p_off
    assert MEMORY_GUIDANCE in p_on


def test_memory_guidance_uses_declarative_phrasing_examples() -> None:
    """Hermes' rule: imperative phrasing in saved memories overrides
    the user's later request. The guidance must show declarative ✓ vs
    imperative ✗ examples so the model picks up on it."""
    assert "User prefers concise responses" in MEMORY_GUIDANCE
    assert "Always respond concisely" in MEMORY_GUIDANCE
    assert "✓" in MEMORY_GUIDANCE
    assert "✗" in MEMORY_GUIDANCE


def test_skills_section_drags_in_anti_refusal() -> None:
    p = build_system_prompt(
        model_id="claude-sonnet-4-6", tool_names=("skill_run",)
    )
    assert SKILLS_GUIDANCE in p
    assert ANTI_REFUSAL in p


def test_per_tool_playbooks_each_gated_independently() -> None:
    p = build_system_prompt(
        model_id="claude-sonnet-4-6", tool_names=("weather", "wiki_search")
    )
    assert WEATHER_PLAYBOOK in p
    assert WIKI_PLAYBOOK in p
    assert WEB_RESEARCH_PLAYBOOK not in p


# ── model-family overlay ─────────────────────────────────────────────


def test_enforcement_appended_for_small_local_model() -> None:
    # Pass at least one tool — enforcement is gated on tools present
    # to avoid the empty-tools verifier-loop pathology measured on
    # the 12-task qwen3.5:9b bench.
    p = build_system_prompt(model_id="qwen3.5:9b", tool_names=("memory_save",))
    assert TOOL_USE_ENFORCEMENT in p


def test_enforcement_skipped_for_frontier_model() -> None:
    p = build_system_prompt(model_id="claude-sonnet-4-6", tool_names=())
    assert TOOL_USE_ENFORCEMENT not in p


def test_enforcement_skipped_when_no_tools_registered() -> None:
    """Bench finding: with zero tools registered the 'MUST emit a
    tool_use block' overlay drives the model to invent fake tools and
    the verifier loops rejecting every turn. Skip the overlay when
    there's nothing to call."""
    p = build_system_prompt(model_id="qwen3.5:9b", tool_names=())
    assert TOOL_USE_ENFORCEMENT not in p


def test_enforcement_appended_when_at_least_one_tool_present() -> None:
    """Single tool is enough to satisfy the gate."""
    p = build_system_prompt(model_id="qwen3.5:9b", tool_names=("memory_save",))
    assert TOOL_USE_ENFORCEMENT in p


# ── channel hint ─────────────────────────────────────────────────────


def test_channel_hint_appended_when_channel_known() -> None:
    p = build_system_prompt(
        model_id="claude-sonnet-4-6", tool_names=(), channel="slack"
    )
    assert PLATFORM_HINTS["slack"] in p


def test_channel_hint_skipped_for_unknown_channel() -> None:
    p = build_system_prompt(
        model_id="claude-sonnet-4-6", tool_names=(), channel="dashboard"
    )
    for hint in PLATFORM_HINTS.values():
        assert hint not in p


def test_whatsapp_and_signal_warn_off_markdown() -> None:
    """Plain-text channels must explicitly tell the model not to use
    markdown — otherwise it hallucinates `**bold**` into the message."""
    for ch in ("whatsapp", "signal"):
        hint = PLATFORM_HINTS[ch].lower()
        assert "markdown" in hint
        assert "not" in hint  # negation present somewhere near markdown


# ── stable section ordering (cache-prefix discipline) ────────────────


def test_section_order_is_stable_across_calls() -> None:
    """The composer must emit sections in a fixed order so an upstream
    prompt cache can match the prefix across turns."""
    p1 = build_system_prompt(
        model_id="qwen3.5:9b",
        tool_names=("memory_save", "skill_run", "weather", "web_search"),
        channel="slack",
    )
    p2 = build_system_prompt(
        model_id="qwen3.5:9b",
        tool_names=("memory_save", "skill_run", "weather", "web_search"),
        channel="slack",
    )
    assert p1 == p2


def test_identity_always_at_top() -> None:
    p = build_system_prompt(
        model_id="qwen3.5:9b",
        tool_names=("memory_save", "skill_run", "weather"),
        channel="slack",
    )
    assert p.startswith(DEFAULT_IDENTITY)


def test_explicit_override_replaces_identity() -> None:
    p = build_system_prompt(
        identity="You are a haiku-only critic. Always respond in 5-7-5.",
        tool_names=(),
    )
    assert p.startswith("You are a haiku-only critic.")
    assert DEFAULT_IDENTITY not in p
