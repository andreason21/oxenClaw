"""`delegate_to_acp` — hand a sub-task off to a frontier ACP server.

This is the **primary** ACP value for oxenClaw: when the local model
(Ollama / gemma / qwen) is too weak for a particular sub-task —
complex coding, multi-file refactors, careful planning — PiAgent can
delegate that *one* turn to a stronger external agent that speaks
ACP, then resume on the local model. It costs us a subprocess hop;
it saves us from having to upgrade the local model.

Wraps `SubprocessAcpRuntime` so a single tool call goes:

    initialize → session/new → session/prompt → (collect text +
    tool_call/update notifications) → done(stopReason) → close

Returns the concatenated assistant text and the stop reason. Tool
events are *summarised* into the result string (count + last status)
rather than streamed back into PiAgent's own session — projecting
them upward as live PiAgent tool_call cards is a follow-up that
needs a hook tap on the parent agent's HookRunner.

Three runtimes are pre-mapped:

  - `claude` → argv `["claude", "acp"]`     (Anthropic Claude Code)
  - `codex`  → argv `["codex", "acp"]`      (OpenAI Codex CLI)
  - `gemini` → argv `["gemini", "acp"]`     (Google Gemini CLI)

Operators can override either by passing an explicit `argv` list
(useful for `oxenclaw acp` itself, for sandboxed builds, or for any
other ACP server reachable via stdio).

The tool is *not* registered by default — the operator opts in via
`oxenclaw/tools_pkg/bundle.py` or by calling
`register_acp_delegation_tool` with a specific runtime allow-list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.acp.subprocess_runtime import AcpWireError, SubprocessAcpRuntime
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEnsureInput,
    AcpRuntimeTurnInput,
)
from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("tools.delegate_to_acp")


_DEFAULT_RUNTIMES: dict[str, list[str]] = {
    "claude": ["claude", "acp"],
    "codex": ["codex", "acp"],
    "gemini": ["gemini", "acp"],
}


class _DelegateArgs(BaseModel):
    model_config = {"extra": "forbid"}

    runtime: Literal["claude", "codex", "gemini", "custom"] = Field(
        ...,
        description=(
            "Frontier ACP runtime to delegate to. 'claude'/'codex'/"
            "'gemini' use the bundled CLI argv; 'custom' requires "
            "`argv` to be provided."
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "Sub-task to hand off. State the goal in one paragraph "
            "— the frontier agent gets no context from this side."
        ),
    )
    argv: list[str] | None = Field(
        None,
        description=(
            "Override the spawn argv. Required when runtime='custom'. "
            "Ignored otherwise. Example: ['python', '-m', "
            "'oxenclaw.acp.server', '--backend', 'fake']."
        ),
    )
    cwd: str | None = Field(
        None,
        description="Optional working directory for the child process.",
    )
    timeout_seconds: float = Field(
        300.0,
        gt=0,
        le=3600,
        description=("Hard cap on the entire delegation (initialize → done). Default 5 min."),
    )


def _resolve_argv(args: _DelegateArgs) -> list[str]:
    if args.runtime == "custom":
        if not args.argv:
            raise ValueError("runtime='custom' requires an explicit `argv` list")
        return list(args.argv)
    if args.argv:
        # Allow override even on a known runtime — operator might
        # have a sandboxed build path.
        return list(args.argv)
    return list(_DEFAULT_RUNTIMES[args.runtime])


async def _run_delegation(args: _DelegateArgs) -> str:
    import asyncio

    try:
        argv = _resolve_argv(args)
    except ValueError as exc:
        return f"[delegate_to_acp/{args.runtime} failed: {exc}]"
    runtime = SubprocessAcpRuntime(argv=argv, backend_id=f"delegate-{args.runtime}", cwd=args.cwd)
    text_chunks: list[str] = []
    tool_count = 0
    last_tool_status: str | None = None
    stop_reason = "stop"
    try:
        # The whole flow runs inside one wait_for so a misbehaving
        # frontier server can't hang us forever.
        async def _flow() -> None:
            nonlocal stop_reason, tool_count, last_tool_status
            handle = await runtime.ensure_session(
                AcpRuntimeEnsureInput(
                    session_key=f"delegate:{args.runtime}",
                    agent="oxenclaw-pi",
                    mode="oneshot",
                    cwd=args.cwd,
                )
            )
            async for ev in runtime.run_turn(
                AcpRuntimeTurnInput(
                    handle=handle,
                    text=args.prompt,
                    mode="prompt",
                    request_id="delegation",
                )
            ):
                if isinstance(ev, AcpEventTextDelta):
                    text_chunks.append(ev.text)
                elif isinstance(ev, AcpEventToolCall):
                    tool_count += 1
                    last_tool_status = ev.status
                elif isinstance(ev, AcpEventDone):
                    stop_reason = ev.stop_reason or "stop"
                elif isinstance(ev, AcpEventError):
                    raise RuntimeError(ev.message)

        await asyncio.wait_for(_flow(), timeout=args.timeout_seconds)
    except TimeoutError:
        return (
            f"[delegate_to_acp/{args.runtime} timeout after "
            f"{args.timeout_seconds:.0f}s — partial text: "
            f"{''.join(text_chunks)[:500]!r}]"
        )
    except AcpWireError as exc:
        return f"[delegate_to_acp/{args.runtime} wire error {exc.code}: {exc.message}]"
    except FileNotFoundError as exc:
        return (
            f"[delegate_to_acp/{args.runtime} CLI not found — "
            f"{exc}. Install the runtime or pass an explicit argv.]"
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("delegate_to_acp failed: runtime=%s", args.runtime)
        return f"[delegate_to_acp/{args.runtime} failed: {exc}]"
    finally:
        await runtime.aclose()

    full_text = "".join(text_chunks)
    summary_bits = [
        f"runtime={args.runtime}",
        f"stopReason={stop_reason}",
    ]
    if tool_count:
        summary_bits.append(f"tool_calls={tool_count} last_status={last_tool_status}")
    head = f"[delegate_to_acp {' '.join(summary_bits)}]\n"
    return head + full_text


def acp_delegate_tool() -> Tool:
    """Build the FunctionTool. Operators register on the agent's
    ToolRegistry to opt in."""

    return FunctionTool(
        name="delegate_to_acp",
        description=(
            "Hand a sub-task to a stronger external agent over ACP "
            "(Agent Client Protocol) when the local model is the wrong "
            "tool — e.g. complex multi-file refactors, careful "
            "long-horizon planning, or anything where the operator "
            "explicitly asks for Claude Code / Codex / Gemini. The "
            "frontier agent runs as a child stdio process; we collect "
            "its assistant text, the final stopReason, and a count of "
            "the tool calls it made along the way. The frontier "
            "doesn't see this side's history — restate the goal in "
            "the `prompt`."
        ),
        input_model=_DelegateArgs,
        handler=_run_delegation,
    )


__all__ = ["acp_delegate_tool"]
