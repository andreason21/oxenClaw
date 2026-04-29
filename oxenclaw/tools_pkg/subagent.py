"""subagents tool — let an agent spawn a child agent for sub-tasks.

Mirrors openclaw `subagents-tool.ts` + `sessions-spawn-tool.ts` +
`sessions-yield-tool.ts`. The parent agent issues a `subagents` tool
call with a task description; the tool builds an isolated child
PiAgent (its own SessionManager + tool subset), runs one full turn,
returns the child's final text.

Why isolated SessionManager? The parent doesn't want sub-task chatter
polluting its own transcript or compaction window. The child writes to
its own session row, and the parent only sees the final summary the
child returned.

Safety: the child inherits the parent's `tools_for_subagents` set
(default: only `web_fetch` + `web_search`); never the parent's full set
unless explicitly opted in. Recursion is capped via `max_depth`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool, ToolRegistry
from oxenclaw.pi import (
    AssistantMessage,
    AuthStorage,
    InMemorySessionManager,
    Model,
    SessionManager,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.auth import resolve_api
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.tools_pkg._arg_aliases import fold_aliases
from oxenclaw.tools_pkg._desc import hermes_desc

logger = get_logger("tools.subagent")


_DEPTH_KEY = "_subagent_depth"


@dataclass
class SubagentConfig:
    """Knobs for the subagents tool factory."""

    model: Model
    auth: AuthStorage
    sessions: SessionManager = field(default_factory=InMemorySessionManager)
    tools: list[Tool] = field(default_factory=list)
    max_depth: int = 2
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    system_prompt: str = (
        "You are a sub-agent. Focus on the single task you are given. "
        "Use tools when helpful. Return a concise, structured answer."
    )


class _SubagentArgs(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "task": ("prompt", "text", "query", "message", "instruction", "goal"),
                "context": ("background", "info", "details", "notes"),
            },
        )

    task: str = Field(..., description="The sub-task to run, in plain language.")
    context: str | None = Field(
        None, description="Optional extra context to seed the sub-agent's prompt."
    )


def subagents_tool(config: SubagentConfig, *, current_depth: int = 0) -> Tool:
    """Build a `subagents` tool bound to `config`.

    `current_depth` is incremented as the parent passes the tool down to
    its children, capped by `config.max_depth` to prevent runaway
    recursion. The parent runner pre-builds the tool with `current_depth=0`.
    """

    async def _h(args: _SubagentArgs) -> str:
        if current_depth >= config.max_depth:
            return (
                f"subagents: refused, recursion depth {current_depth} would "
                f"exceed max_depth={config.max_depth}"
            )
        # Compose the child's user message from task + context.
        prompt_lines = [f"Task: {args.task}"]
        if args.context:
            prompt_lines.append("")
            prompt_lines.append(f"Context:\n{args.context}")
        user_text = "\n".join(prompt_lines)

        # Build tool registry the child sees: explicitly-shared tools +
        # a *new* subagents tool with depth+1 so a child can spawn a
        # grandchild but not infinitely.
        child_tools: list[Tool] = list(config.tools)
        child_tools.append(subagents_tool(config, current_depth=current_depth + 1))

        api = await resolve_api(config.model, config.auth)
        try:
            result = await run_agent_turn(
                model=config.model,
                api=api,
                system=config.system_prompt,
                history=[UserMessage(content=user_text)],
                tools=child_tools,
                config=config.runtime,
            )
        except Exception as exc:
            logger.exception("subagent run failed")
            return f"subagents: child failed: {exc}"

        if not isinstance(result.final_message, AssistantMessage):
            return "subagents: child produced no message"
        text_blocks = [b.text for b in result.final_message.content if isinstance(b, TextContent)]
        text = "\n".join(t for t in text_blocks if t).strip()
        if not text:
            text = f"(child returned no text; stop_reason={result.stopped_reason})"
        return text

    return FunctionTool(
        name="subagents",
        description=hermes_desc(
            "Spawn a sub-agent to handle a focused sub-task in isolation "
            "(separate session + restricted tool set). Returns the child's "
            "final text answer.",
            when_use=[
                "the sub-task is self-contained and ≥3 tool calls",
                "you want sub-task chatter kept out of the parent transcript",
                "the sub-task can run with the restricted tool set",
            ],
            when_skip=[
                "you can answer in one tool call yourself (overhead not worth it)",
                "the sub-task needs a tool the child doesn't have",
                "current depth is at max_depth (will be refused)",
            ],
            alternatives={
                "skill_run": "running a documented skill script",
                "shell": "single ad-hoc command",
            },
            notes=(
                "The `task` field is self-contained — the child has no "
                "memory of the parent's conversation."
            ),
        ),
        input_model=_SubagentArgs,
        handler=_h,
    )


def add_subagent_tool(
    registry: ToolRegistry,
    *,
    config: SubagentConfig,
) -> None:
    """Convenience: build the subagents tool and register it on `registry`."""
    registry.register(subagents_tool(config))


__all__ = ["SubagentConfig", "add_subagent_tool", "subagents_tool"]
