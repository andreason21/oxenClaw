"""Single inference attempt.

One `run_attempt` call = one POST to the provider, one stream consumption,
one fully-assembled `AssistantMessage`. The caller (run.py) decides whether
to loop again based on the assembled message's `stop_reason`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sampyclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ThinkingBlock,
    ToolUseBlock,
)
from sampyclaw.pi.models import Context, Model
from sampyclaw.pi.run.runtime import RuntimeConfig
from sampyclaw.pi.streaming import (
    AssistantMessageEvent,
    ErrorEvent,
    SimpleStreamOptions,
    StopEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
    stream_simple,
)


@dataclass
class AttemptResult:
    """One stream's assembled output."""

    message: AssistantMessage
    usage: dict[str, Any] | None = None
    error: ErrorEvent | None = None
    events: list[AssistantMessageEvent] = field(default_factory=list)


async def run_attempt(
    *,
    model: Model,
    api: Any,
    system: str | None,
    messages: list[Any],
    tools: list[Any],
    config: RuntimeConfig,
    on_event: Any | None = None,
) -> AttemptResult:
    """Stream one provider call and assemble the final AssistantMessage.

    `on_event` (optional) is a coroutine `(event) -> None` invoked for every
    streamed event; the run loop uses it to push deltas to a UI/dashboard
    while the attempt is in flight.
    """
    ctx = Context(
        model=model,
        api=api,
        system=system,
        messages=list(messages),
        tools=list(tools),
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        thinking=config.thinking,
        cache_control_breakpoints=config.cache_control_breakpoints,
    )
    opts = SimpleStreamOptions(
        abort_event=config.abort_event,
        timeout_seconds=config.timeout_seconds,
        include_usage=True,
        extra_params=dict(config.extra_params),
    )

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    thinking_signature: str | None = None
    tool_buf: dict[str, dict[str, str]] = {}  # id → {name, args}
    tool_order: list[str] = []
    usage: dict[str, Any] | None = None
    error: ErrorEvent | None = None
    stop_reason: str | None = None
    events: list[AssistantMessageEvent] = []

    async for event in stream_simple(ctx, opts):
        events.append(event)
        if on_event is not None:
            await on_event(event)

        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.delta)
        elif isinstance(event, ThinkingDeltaEvent):
            if event.delta:
                thinking_parts.append(event.delta)
            if event.signature:
                thinking_signature = event.signature
        elif isinstance(event, ToolUseStartEvent):
            tool_buf[event.id] = {"name": event.name, "args": ""}
            tool_order.append(event.id)
        elif isinstance(event, ToolUseInputDeltaEvent):
            slot = tool_buf.setdefault(event.id, {"name": "", "args": ""})
            slot["args"] += event.input_delta
        elif isinstance(event, ToolUseEndEvent):
            pass  # Args fully accumulated; finalize at message assembly.
        elif isinstance(event, UsageEvent):
            usage = event.usage
        elif isinstance(event, StopEvent):
            stop_reason = event.reason
        elif isinstance(event, ErrorEvent):
            error = event
            break

    # Assemble final AssistantMessage.
    content: list[Any] = []
    if thinking_parts:
        content.append(
            ThinkingBlock(
                thinking="".join(thinking_parts),
                signature=thinking_signature,
            )
        )
    if text_parts:
        content.append(TextContent(text="".join(text_parts)))
    for tid in tool_order:
        slot = tool_buf[tid]
        try:
            input_obj = json.loads(slot["args"]) if slot["args"] else {}
        except json.JSONDecodeError:
            # Keep the raw args under a `_raw` key so the run loop can
            # feed back a JSON-error tool result and let the model retry.
            input_obj = {"_raw": slot["args"], "_parse_error": True}
        content.append(ToolUseBlock(id=tid, name=slot["name"], input=input_obj))

    message = AssistantMessage(
        content=content or [TextContent(text="")],
        stop_reason=stop_reason or ("error" if error else "end_turn"),
        usage=usage,
    )
    return AttemptResult(message=message, usage=usage, error=error, events=events)


__all__ = ["AttemptResult", "run_attempt"]
