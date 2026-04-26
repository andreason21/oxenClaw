"""Agent harness, tool invocation, inference loop. Port of openclaw src/agents/*."""

from oxenclaw.agents.base import Agent, AgentContext
from oxenclaw.agents.builtin_tools import default_tools, echo_tool, get_time_tool
from oxenclaw.agents.dispatch import Dispatcher, SendCallable
from oxenclaw.agents.echo import EchoAgent
from oxenclaw.agents.factory import SUPPORTED_PROVIDERS, UnknownProvider, build_agent
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.agents.local_agent import LocalAgent
from oxenclaw.agents.registry import (
    AgentRegistry,
    session_key_for,
    session_key_for_envelope,
)
from oxenclaw.agents.tools import FunctionTool, Tool, ToolRegistry

__all__ = [
    "SUPPORTED_PROVIDERS",
    "Agent",
    "AgentContext",
    "AgentRegistry",
    "ConversationHistory",
    "Dispatcher",
    "EchoAgent",
    "FunctionTool",
    "LocalAgent",
    "SendCallable",
    "Tool",
    "ToolRegistry",
    "UnknownProvider",
    "build_agent",
    "default_tools",
    "echo_tool",
    "get_time_tool",
    "session_key_for",
    "session_key_for_envelope",
]
