"""Tests for the CLI agent-factory routing."""

from __future__ import annotations

import pytest
import typer

from oxenclaw.agents import EchoAgent
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.cli.gateway_cmd import build_agent


def test_build_echo_agent() -> None:
    agent = build_agent(agent_id="assistant", provider="echo")
    assert isinstance(agent, EchoAgent)
    assert agent.id == "assistant"


def test_build_anthropic_agent_with_default_tools() -> None:
    """`--provider anthropic` is a pi alias now; default tools still wire.
    Default registry now includes the openclaw-style fs/shell/process/
    plan bundle alongside echo/get_time."""
    agent = build_agent(agent_id="assistant", provider="anthropic")
    assert isinstance(agent, PiAgent)
    assert agent.id == "assistant"
    names = set(agent._tools.names())
    for required in (
        "echo",
        "get_time",
        "read_file",
        "list_dir",
        "grep",
        "glob",
        "read_pdf",
        "write_file",
        "edit",
        "shell",
        "process",
        "update_plan",
    ):
        assert required in names, f"missing {required!r}: {sorted(names)}"


def test_unknown_provider_raises_bad_parameter() -> None:
    with pytest.raises(typer.BadParameter):
        build_agent(agent_id="x", provider="gpt5")
