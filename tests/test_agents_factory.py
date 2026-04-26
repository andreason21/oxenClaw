"""Tests for build_agent factory."""

from __future__ import annotations

import pytest

from sampyclaw.agents import (
    AnthropicAgent,
    EchoAgent,
    LocalAgent,
    SUPPORTED_PROVIDERS,
    UnknownProvider,
    build_agent,
)


def test_build_echo() -> None:
    agent = build_agent(agent_id="a", provider="echo")
    assert isinstance(agent, EchoAgent)


def test_build_anthropic_defaults_to_builtin_tools() -> None:
    agent = build_agent(agent_id="a", provider="anthropic")
    assert isinstance(agent, AnthropicAgent)
    assert sorted(agent._tools.names()) == ["echo", "get_time"]


def test_build_anthropic_custom_system_prompt_and_model() -> None:
    agent = build_agent(
        agent_id="a",
        provider="anthropic",
        system_prompt="You are brief.",
        model="claude-haiku-4-5-20251001",
    )
    assert agent._system_prompt == "You are brief."
    assert agent._model == "claude-haiku-4-5-20251001"


def test_build_local_defaults_target_tool_capable_ollama_model() -> None:
    agent = build_agent(agent_id="a", provider="local")
    assert isinstance(agent, LocalAgent)
    assert sorted(agent._tools.names()) == ["echo", "get_time"]
    # Default must be tool-capable (gemma3 has weak/no tool support;
    # gemma4 restored function calling so it's the new default).
    assert agent._model == "gemma4:latest"
    assert agent._base_url.endswith("11434/v1")


def test_build_local_custom_endpoint_and_model() -> None:
    agent = build_agent(
        agent_id="a",
        provider="local",
        model="llama3.2:8b",
        base_url="http://gpu.local:8080/v1",
        api_key="sk-fake",
        system_prompt="You are terse.",
    )
    assert isinstance(agent, LocalAgent)
    assert agent._model == "llama3.2:8b"
    assert agent._base_url == "http://gpu.local:8080/v1"
    assert agent._api_key == "sk-fake"
    assert agent._system_prompt == "You are terse."


def test_build_local_ignores_base_url_on_anthropic() -> None:
    # base_url is local-only; anthropic should ignore it, not crash.
    agent = build_agent(
        agent_id="a", provider="anthropic", base_url="ignored"
    )
    assert isinstance(agent, AnthropicAgent)


def test_unknown_provider_raises() -> None:
    with pytest.raises(UnknownProvider):
        build_agent(agent_id="a", provider="gpt5")


def test_supported_providers_contains_known() -> None:
    assert set(SUPPORTED_PROVIDERS) >= {"echo", "anthropic", "local", "vllm"}


def test_build_vllm_uses_strict_openai_flavor_and_default_port() -> None:
    """`--provider vllm` lands on LocalAgent in vllm flavor, defaulting to
    vLLM's canonical 8000 port instead of Ollama's 11434."""
    agent = build_agent(agent_id="a", provider="vllm", model="meta-llama/Llama-3.1-8B-Instruct")
    assert isinstance(agent, LocalAgent)
    assert agent._flavor == "vllm"
    assert agent._base_url == "http://127.0.0.1:8000/v1"
    assert agent._model == "meta-llama/Llama-3.1-8B-Instruct"
    # vLLM has weights resident — no warmup ping needed.
    assert agent._warmup_pending is False


def test_build_vllm_custom_endpoint_and_api_key() -> None:
    """Internal vLLM box: custom URL + bearer token override the defaults."""
    agent = build_agent(
        agent_id="a",
        provider="vllm",
        model="qwen2.5:32b",
        base_url="http://internal-vllm.lan:8000/v1",
        api_key="sk-internal",
    )
    assert isinstance(agent, LocalAgent)
    assert agent._flavor == "vllm"
    assert agent._base_url == "http://internal-vllm.lan:8000/v1"
    assert agent._api_key == "sk-internal"
