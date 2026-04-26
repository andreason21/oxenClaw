"""Tests for build_agent factory.

Post-rc.15: every catalog provider routes through PiAgent. The pre-pi
LocalAgent / AnthropicAgent class routes were removed from the factory.
Legacy provider names (`local`, `pi`) still work via the alias map but
emit a deprecation warning.
"""

from __future__ import annotations

import logging

import pytest

from oxenclaw.agents import (
    SUPPORTED_PROVIDERS,
    EchoAgent,
    UnknownProvider,
    build_agent,
)
from oxenclaw.agents.factory import (
    CATALOG_PROVIDERS,
    LEGACY_ALIASES,
    PROVIDER_DEFAULT_MODELS,
)
from oxenclaw.agents.pi_agent import PiAgent


def test_supported_providers_is_catalog_plus_echo() -> None:
    assert set(SUPPORTED_PROVIDERS) == set(CATALOG_PROVIDERS) | {"echo"}


def test_catalog_providers_match_pi_registrations() -> None:
    """Every catalog provider id must have a registered stream wrapper.
    The reverse — extra wrappers without a matching catalog id — is
    legitimate (plugin / test stubs), so this is a one-way subset check
    rather than equality."""
    import oxenclaw.pi.providers  # noqa: F401  registers wrappers
    from oxenclaw.pi.streaming import _PROVIDER_STREAMS  # type: ignore[attr-defined]

    missing = set(CATALOG_PROVIDERS) - set(_PROVIDER_STREAMS.keys())
    assert not missing, f"advertised but not wired: {sorted(missing)}"


def test_build_echo_returns_echo_agent() -> None:
    agent = build_agent(agent_id="a", provider="echo")
    assert isinstance(agent, EchoAgent)


def test_build_anthropic_uses_claude_default() -> None:
    """`--provider anthropic` resolves to PiAgent with the catalog's
    default Anthropic model (cheap / latest mid-tier)."""
    agent = build_agent(agent_id="a", provider="anthropic")
    assert isinstance(agent, PiAgent)
    assert agent._model.id == PROVIDER_DEFAULT_MODELS["anthropic"]
    assert agent._model.provider == "anthropic"


def test_build_anthropic_with_explicit_model() -> None:
    agent = build_agent(
        agent_id="a",
        provider="anthropic",
        system_prompt="You are brief.",
        model="claude-haiku-4-5-20251001",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.id == "claude-haiku-4-5-20251001"
    assert agent._system_prompt == "You are brief."


def test_build_ollama_picks_gemma4_default() -> None:
    """openclaw-style: `--provider ollama` lands on PiAgent with the
    catalog's tool-capable gemma4 default."""
    agent = build_agent(agent_id="a", provider="ollama")
    assert isinstance(agent, PiAgent)
    assert agent._model.id == PROVIDER_DEFAULT_MODELS["ollama"]
    assert agent._model.provider == "ollama"


def test_build_local_alias_maps_to_ollama_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    """`--provider local` is a legacy alias kept for back-compat with
    pre-rc.15 config.yaml files. Should produce the same agent as
    `--provider ollama` and log a deprecation warning."""
    with caplog.at_level(logging.WARNING):
        agent = build_agent(agent_id="a", provider="local")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "ollama"
    assert any("legacy alias" in r.getMessage() for r in caplog.records)


def test_build_pi_alias_also_warns_and_maps_to_ollama(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        agent = build_agent(agent_id="a", provider="pi")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "ollama"
    assert any("legacy alias" in r.getMessage() for r in caplog.records)


def test_build_vllm_with_custom_model_synthesises_registry_entry() -> None:
    """vLLM users typically run a model that isn't in the catalog
    (`meta-llama/Llama-3.1-8B-Instruct`, fine-tunes, etc.). The factory
    synthesises a transient registry entry instead of failing."""
    agent = build_agent(
        agent_id="a",
        provider="vllm",
        model="meta-llama/Llama-3.1-8B-Instruct",
        base_url="http://internal-vllm.lan:8000/v1",
        api_key="sk-internal",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.id == "meta-llama/Llama-3.1-8B-Instruct"
    assert agent._model.provider == "vllm"
    assert agent._model.extra.get("base_url") == "http://internal-vllm.lan:8000/v1"


def test_build_with_base_url_override_on_catalog_model() -> None:
    """Catalog model + custom base_url → registry copy with override."""
    agent = build_agent(
        agent_id="a",
        provider="ollama",
        model="gemma4:latest",
        base_url="http://gpu.local:11434/v1",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.extra.get("base_url") == "http://gpu.local:11434/v1"


def test_unknown_provider_raises() -> None:
    with pytest.raises(UnknownProvider):
        build_agent(agent_id="a", provider="gpt5")


def test_legacy_aliases_cover_pre_rc15_names() -> None:
    """The two main pre-rc.15 names (`local`, `pi`) must remain
    accepted so existing config.yaml + dashboards keep working."""
    assert "local" in LEGACY_ALIASES
    assert "pi" in LEGACY_ALIASES
    assert LEGACY_ALIASES["local"] == "ollama"


def test_default_tools_are_registered_when_none_passed() -> None:
    agent = build_agent(agent_id="a", provider="anthropic")
    assert sorted(agent._tools.names()) == ["echo", "get_time"]
