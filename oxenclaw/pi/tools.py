"""AgentTool protocol + tool-call request/result types.

Mirrors `@mariozechner/pi-agent-core` `AgentTool`. The shape is intentionally
narrow: a name, a description, an input JSON-Schema, and an `execute()`
coroutine. Providers serialise the tool list differently (Anthropic uses
`name + description + input_schema`; OpenAI uses `function: {name, ...}`)
— wrappers in `oxenclaw.pi.providers.*` do that translation.

`AgentTool` is intentionally compatible with `oxenclaw.agents.tools.Tool`
so existing tools can plug in without rewriting. The runtime tool loop
calls `execute(args, ctx)` where `ctx` is the per-attempt context (lets
tools access cancellation tokens, attempt-scoped state, the session id).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentTool(Protocol):
    """A tool the agent may invoke during a turn.

    Compatible with the existing `oxenclaw.agents.tools.Tool` Protocol —
    `execute()` accepts the validated args dict and returns a string. The
    optional second `ctx` arg is a forward-compat slot; old tools that
    don't take it remain valid because the runtime passes args positionally
    only when the signature accepts them.
    """

    name: str
    description: str

    @property
    def input_schema(self) -> dict[str, Any]: ...

    async def execute(self, args: dict[str, Any]) -> str: ...


@dataclass(frozen=True)
class ToolUseRequest:
    """One tool invocation requested by the assistant."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Outcome of running one ToolUseRequest."""

    id: str
    name: str
    output: str
    is_error: bool = False
    duration_seconds: float = 0.0


# Optional contextual second arg some advanced tools opt into.
ToolExecuteWithCtx = Callable[[dict[str, Any], "ToolCallContext"], Awaitable[str]]


@dataclass(frozen=True)
class ToolCallContext:
    """Per-attempt context handed to tool implementations that opt in."""

    session_id: str
    attempt_id: str
    abort_event: Any  # asyncio.Event — typed loose to keep this module sync-safe


__all__ = [
    "AgentTool",
    "ToolCallContext",
    "ToolExecuteWithCtx",
    "ToolExecutionResult",
    "ToolUseRequest",
]
