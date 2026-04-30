"""Single inference attempt.

One `run_attempt` call = one POST to the provider, one stream consumption,
one fully-assembled `AssistantMessage`. The caller (run.py) decides whether
to loop again based on the assembled message's `stop_reason`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ThinkingBlock,
    ToolUseBlock,
)
from oxenclaw.pi.models import Context, Model
from oxenclaw.pi.run.json_repair import repair_and_parse
from oxenclaw.pi.run.runtime import RuntimeConfig
from oxenclaw.pi.streaming import (
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
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run.attempt")


@dataclass
class AttemptResult:
    """One stream's assembled output."""

    message: AssistantMessage
    usage: dict[str, Any] | None = None
    error: ErrorEvent | None = None
    events: list[AssistantMessageEvent] = field(default_factory=list)
    # True once at least one TextDeltaEvent reached `on_event` (i.e. user-
    # visible text was streamed to the channel). The run loop reads this
    # to decide whether a transport-level error is safely retryable in
    # the same attempt without duplicating output to the user.
    text_emitted: bool = False


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
    # Stash the optional rate_limit_tracker on Context.extra so the
    # provider stream wrappers can call .record(...) after a successful
    # response without us threading it through SimpleStreamOptions.
    ctx_extra: dict[str, Any] = {}
    if getattr(config, "rate_limit_tracker", None) is not None:
        ctx_extra["rate_limit_tracker"] = config.rate_limit_tracker
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
        extra=ctx_extra,
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
    text_emitted = False  # see AttemptResult.text_emitted

    async for event in stream_simple(ctx, opts):
        events.append(event)
        if on_event is not None:
            await on_event(event)

        if isinstance(event, TextDeltaEvent):
            text_parts.append(event.delta)
            if event.delta:
                # Mark as emitted only after a non-empty delta — empty
                # deltas (most providers send a leading "") shouldn't
                # block silent retry of a stream that hasn't actually
                # produced visible output yet.
                text_emitted = True
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
        # Guard against an out-of-order delta race: if input_delta arrived
        # before the start event, the slot was created via setdefault with
        # an empty `name`. We can't dispatch to a nameless tool, so flag
        # the args as parse-error so run.py feeds the model a structured
        # nudge instead of silently registering an unknown-tool failure.
        if not slot["name"]:
            content.append(
                ToolUseBlock(
                    id=tid,
                    name="_unknown",
                    input={"_raw": slot["args"], "_parse_error": True},
                )
            )
            logger.warning(
                "tool_use frame race: input_delta arrived before tool_use_start id=%s args_len=%d",
                tid,
                len(slot["args"]),
            )
            continue
        if not slot["args"]:
            input_obj: Any = {}
        else:
            parsed, repair = repair_and_parse(slot["args"])
            if parsed is None:
                # Total failure — fall back to the legacy _parse_error
                # signal so the run loop can feed a JSON-error tool
                # result back and let the model retry.
                input_obj = {"_raw": slot["args"], "_parse_error": True}
            else:
                if repair:
                    logger.info(
                        "json-repair applied to tool=%s id=%s repair=%s",
                        slot["name"],
                        tid,
                        repair,
                    )
                input_obj = parsed
        content.append(ToolUseBlock(id=tid, name=slot["name"], input=input_obj))

    message = AssistantMessage(
        content=content or [TextContent(text="")],
        stop_reason=stop_reason or ("error" if error else "end_turn"),
        usage=usage,
    )
    return AttemptResult(
        message=message,
        usage=usage,
        error=error,
        events=events,
        text_emitted=text_emitted,
    )


__all__ = ["AttemptResult", "run_attempt"]
