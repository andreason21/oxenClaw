"""sessions_spawn(runtime: 'acp') tool — drive Claude/Codex/Gemini CLI.

Mirrors openclaw `sessions_spawn(runtime: "acp")`. Lets the model
delegate a sub-task to a stronger external CLI when:

  - The local model is too weak for the task (e.g. complex refactor
    via Claude Code).
  - The user explicitly asks "do this in Claude Code / Codex /
    Gemini".

One-shot only: prompt in, completion out. Persistent ACP sessions
will arrive in a later port.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.acp_subprocess import spawn_acp
from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.tools_pkg._arg_aliases import fold_aliases
from oxenclaw.tools_pkg._desc import hermes_desc


class _AcpSpawnArgs(BaseModel):
    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "runtime": ("backend", "engine", "agent", "provider", "cli"),
                "prompt": ("text", "task", "query", "message", "instruction", "goal"),
            },
        )

    runtime: Literal["claude", "codex", "gemini"] = Field(
        ...,
        description=(
            "External AI CLI runtime: 'claude' (Claude Code), "
            "'codex' (OpenAI Codex CLI), or 'gemini' (Gemini CLI)."
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description="Task prompt forwarded to the CLI as a positional arg.",
    )
    timeout_seconds: float = Field(
        120.0,
        gt=0,
        le=600,
        description="Hard cap on the CLI invocation. Default 120s.",
    )
    cwd: str | None = Field(
        None,
        description=(
            "Optional working directory for the spawned CLI. Defaults to the gateway's CWD."
        ),
    )


def acp_spawn_tool() -> Tool:
    async def _h(args: _AcpSpawnArgs) -> str:
        result = await spawn_acp(
            runtime=args.runtime,
            prompt=args.prompt,
            cwd=args.cwd,
            timeout_seconds=args.timeout_seconds,
        )
        if result.error:
            return f"acp_spawn error: {result.error}"
        if result.timed_out:
            return f"acp_spawn timeout after {result.duration_seconds:.1f}s"
        head = (
            f"[{result.runtime}/{result.cli} exit={result.exit_code} "
            f"dur={result.duration_seconds:.1f}s]\n"
        )
        body = (
            result.stdout
            if result.exit_code == 0
            else (f"{result.stdout}\n[stderr]\n{result.stderr}")
        )
        return head + body

    return FunctionTool(
        name="sessions_spawn",
        description=hermes_desc(
            "Delegate a one-shot prompt to an external AI CLI (Claude "
            "Code / Codex / Gemini) and return its stdout.",
            when_use=[
                "the user explicitly asks for Claude Code / Codex / Gemini",
                "the local model is too weak for the sub-task",
            ],
            when_skip=[
                "you can do the task locally (cheaper / faster)",
                "you need a persistent ACP session (use delegate_to_acp)",
            ],
            alternatives={
                "delegate_to_acp": "richer ACP delegation w/ tool-call summary",
                "subagents": "local isolated child agent",
            },
            notes="The CLI sees no parent context — restate the goal fully.",
        ),
        input_model=_AcpSpawnArgs,
        handler=_h,
    )


__all__ = ["acp_spawn_tool"]
