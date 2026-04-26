"""Tests for the CLI agent-factory routing."""

from __future__ import annotations

import pytest
import typer

from sampyclaw.agents import EchoAgent
from sampyclaw.agents.pi_agent import PiAgent
from sampyclaw.cli.gateway_cmd import build_agent


def test_build_echo_agent() -> None:
    agent = build_agent(agent_id="assistant", provider="echo")
    assert isinstance(agent, EchoAgent)
    assert agent.id == "assistant"


def test_build_anthropic_agent_with_default_tools() -> None:
    """`--provider anthropic` is a pi alias now; default tools still wire."""
    agent = build_agent(agent_id="assistant", provider="anthropic")
    assert isinstance(agent, PiAgent)
    assert agent.id == "assistant"
    assert sorted(agent._tools.names()) == ["echo", "get_time"]


def test_unknown_provider_raises_bad_parameter() -> None:
    with pytest.raises(typer.BadParameter):
        build_agent(agent_id="x", provider="gpt5")
