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

from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.agents.acp_subprocess import spawn_acp
from oxenclaw.agents.tools import FunctionTool, Tool


class _AcpSpawnArgs(BaseModel):
    model_config = {"extra": "forbid"}
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
        description=(
            "Delegate a one-shot prompt to an external AI CLI "
            "(Claude Code / Codex / Gemini). Use when the local model "
            "is the wrong tool for the job — e.g. user explicitly "
            "asks for Claude Code, or the task needs a stronger "
            "frontier model. Returns the CLI's stdout."
        ),
        input_model=_AcpSpawnArgs,
        handler=_h,
    )


__all__ = ["acp_spawn_tool"]
