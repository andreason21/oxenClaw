"""Agent harness, tool invocation, inference loop. Port of openclaw src/agents/*."""

from sampyclaw.agents.anthropic_agent import (
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    AnthropicAgent,
)
from sampyclaw.agents.base import Agent, AgentContext
from sampyclaw.agents.builtin_tools import default_tools, echo_tool, get_time_tool
from sampyclaw.agents.dispatch import Dispatcher, SendCallable
from sampyclaw.agents.echo import EchoAgent
from sampyclaw.agents.factory import SUPPORTED_PROVIDERS, UnknownProvider, build_agent
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.agents.local_agent import LocalAgent
from sampyclaw.agents.registry import (
    AgentRegistry,
    session_key_for,
    session_key_for_envelope,
)
from sampyclaw.agents.tools import FunctionTool, Tool, ToolRegistry

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_SYSTEM_PROMPT",
    "SUPPORTED_PROVIDERS",
    "Agent",
    "AgentContext",
    "AgentRegistry",
    "AnthropicAgent",
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
