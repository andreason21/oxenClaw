"""Multi-attempt run loop.

Translates openclaw `pi-embedded-runner/run.ts` (~2.3K LOC) into a focused
Python coroutine: drive `run_attempt` repeatedly, executing tool calls
between turns, retrying on transient errors, and stopping when the
assistant emits a non-tool stop_reason or hits the iteration cap.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
)
from oxenclaw.pi.models import Model
from oxenclaw.pi.run.attempt import AttemptResult, run_attempt
from oxenclaw.pi.run.runtime import RuntimeConfig
from oxenclaw.pi.tools import AgentTool, ToolExecutionResult
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run")


@dataclass
class TurnResult:
    """Outcome of a complete agent turn (one user message → final assistant)."""

    final_message: AssistantMessage
    appended_messages: list[Any] = field(default_factory=list)
    attempts: list[AttemptResult] = field(default_factory=list)
    tool_executions: list[ToolExecutionResult] = field(default_factory=list)
    usage_total: dict[str, int] = field(default_factory=dict)
    stopped_reason: str = "end_turn"


def _accumulate_usage(total: dict[str, int], delta: dict[str, Any] | None) -> None:
    if not delta:
        return
    for key, value in delta.items():
        if isinstance(value, (int, float)):
            total[key] = int(total.get(key, 0) + value)


def _backoff_delay(attempt: int, *, initial: float, cap: float) -> float:
    base = min(cap, initial * (2 ** max(0, attempt - 1)))
    return random.uniform(0, base)


async def _execute_tool_safely(tool: AgentTool, request: ToolUseBlock) -> ToolExecutionResult:
    """Run one tool with timing + structured error capture."""
    started = time.monotonic()
    if request.input.get("_parse_error"):
        # The attempt assembler couldn't parse the streamed JSON. Feed the
        # raw payload back so the model self-corrects on the next iteration.
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=(
                "tool error: arguments were not valid JSON. "
                f"Raw payload: {request.input.get('_raw', '')!r}. "
                "Re-emit with a JSON object literal."
            ),
            is_error=True,
            duration_seconds=time.monotonic() - started,
        )
    try:
        out = await tool.execute(request.input)
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=out if isinstance(out, str) else json.dumps(out),
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        logger.exception("tool %s raised", request.name)
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=f"tool error: {exc}",
            is_error=True,
            duration_seconds=time.monotonic() - started,
        )


async def _run_tools(
    requests: list[ToolUseBlock],
    tools_by_name: dict[str, AgentTool],
    *,
    parallel: bool,
) -> list[ToolExecutionResult]:
    if not requests:
        return []

    async def _one(req: ToolUseBlock) -> ToolExecutionResult:
        tool = tools_by_name.get(req.name)
        if tool is None:
            return ToolExecutionResult(
                id=req.id,
                name=req.name,
                output=f"tool {req.name!r} is not registered",
                is_error=True,
            )
        return await _execute_tool_safely(tool, req)

    if parallel:
        return list(await asyncio.gather(*(_one(r) for r in requests)))
    out: list[ToolExecutionResult] = []
    for r in requests:
        out.append(await _one(r))
    return out


async def run_agent_turn(
    *,
    model: Model,
    api: Any,
    system: str | None,
    history: list[Any],
    tools: list[AgentTool],
    config: RuntimeConfig,
    on_event: Any | None = None,
) -> TurnResult:
    """Drive the full agent turn until end_turn / cap / abort.

    `history` is the running message list; the loop appends to a copy and
    returns the new entries via `TurnResult.appended_messages` so callers
    can persist them (or roll back on error).
    """
    tools_by_name: dict[str, AgentTool] = {t.name: t for t in tools}
    working: list[Any] = list(history)
    appended: list[Any] = []
    attempts: list[AttemptResult] = []
    executions: list[ToolExecutionResult] = []
    usage_total: dict[str, int] = {}
    stop_reason = "end_turn"

    for _iteration in range(config.max_tool_iterations):
        if config.abort_event is not None and config.abort_event.is_set():
            abort_msg = AssistantMessage(
                content=[TextContent(text="(aborted)")],
                stop_reason="abort",
            )
            return TurnResult(
                final_message=abort_msg,
                appended_messages=appended,
                attempts=attempts,
                tool_executions=executions,
                usage_total=usage_total,
                stopped_reason="abort",
            )

        # Retry inner loop for transient errors.
        retry = 0
        while True:
            result = await run_attempt(
                model=model,
                api=api,
                system=system,
                messages=working,
                tools=tools,
                config=config,
                on_event=on_event,
            )
            attempts.append(result)
            if result.error is None:
                break
            if not result.error.retryable or retry >= config.max_retries:
                stop_reason = "error"
                # Even on terminal error, surface what we got so callers can log.
                appended.append(result.message)
                return TurnResult(
                    final_message=result.message,
                    appended_messages=appended,
                    attempts=attempts,
                    tool_executions=executions,
                    usage_total=usage_total,
                    stopped_reason=stop_reason,
                )
            retry += 1
            delay = _backoff_delay(retry, initial=config.backoff_initial, cap=config.backoff_max)
            logger.warning(
                "transient error (retry %d/%d after %.2fs): %s",
                retry,
                config.max_retries,
                delay,
                result.error.message,
            )
            await asyncio.sleep(delay)

        msg = result.message
        appended.append(msg)
        working.append(msg)
        _accumulate_usage(usage_total, result.usage)

        tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
        if not tool_uses or msg.stop_reason not in ("tool_use", "tool_calls"):
            stop_reason = msg.stop_reason or "end_turn"
            return TurnResult(
                final_message=msg,
                appended_messages=appended,
                attempts=attempts,
                tool_executions=executions,
                usage_total=usage_total,
                stopped_reason=stop_reason,
            )

        results = await _run_tools(tool_uses, tools_by_name, parallel=config.parallel_tools)
        executions.extend(results)
        tr_msg = ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id=r.id, content=r.output, is_error=r.is_error)
                for r in results
            ]
        )
        appended.append(tr_msg)
        working.append(tr_msg)

    # Iteration cap hit.
    cap_msg = AssistantMessage(
        content=[TextContent(text="(stopped: reached max tool iterations without a final answer)")],
        stop_reason="iteration_cap",
    )
    if config.soft_iteration_cap:
        appended.append(cap_msg)
    return TurnResult(
        final_message=cap_msg,
        appended_messages=appended,
        attempts=attempts,
        tool_executions=executions,
        usage_total=usage_total,
        stopped_reason="iteration_cap",
    )


__all__ = ["TurnResult", "run_agent_turn"]
