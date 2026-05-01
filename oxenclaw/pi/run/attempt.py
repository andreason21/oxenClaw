"""Single inference attempt.

One `run_attempt` call = one POST to the provider, one stream consumption,
one fully-assembled `AssistantMessage`. The caller (run.py) decides whether
to loop again based on the assembled message's `stop_reason`.
"""

from __future__ import annotations

import re
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


# Some llama.cpp / vLLM builds leak `<think>...</think>` tags into the
# visible text stream when the runtime's reasoning-format feature is
# disabled (or the model emits them outside the chat template). Strip
# them at message-assembly time so:
#   1. The user-visible TextContent doesn't contain raw thinking.
#   2. `stop_recovery.is_length_truncation` can correctly classify a
#      thinking-only turn as recoverable (visible text==empty after
#      strip ⇒ matches the thinking-only natural-stop pattern).
# Captured thinking is preserved on the ThinkingBlock so observability
# / token accounting still has it.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _split_thinking_tags(text: str) -> tuple[str, str]:
    """Return `(visible_text, captured_thinking)` from a possibly-leaky stream.

    Idempotent on inputs that don't contain `<think>` tags.
    """
    if "<think" not in text.lower():
        return text, ""
    captured: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        captured.append(match.group(0))
        return ""

    visible = _THINK_TAG_RE.sub(_capture, text)
    return visible.strip(), "\n".join(captured)


def default_max_tokens_for(model: Model) -> int:
    """Model-aware ``num_predict`` fallback when nothing is pinned.

    Thinking models (qwen3.5, deepseek-r1, ...) burn a large share of
    their output budget on hidden tokens that never reach the user, so
    we give them ~4× the headroom of plain instruct models. The caps
    are intentionally below ``model.max_output_tokens`` — operators who
    want to use the full ceiling set ``RuntimeConfig.max_tokens``
    explicitly.
    """
    cap = max(256, model.max_output_tokens)
    return min(cap, 4096) if model.supports_thinking else min(cap, 1024)


async def run_attempt(
    *,
    model: Model,
    api: Any,
    system: str | None,
    messages: list[Any],
    tools: list[Any],
    config: RuntimeConfig,
    on_event: Any | None = None,
    max_tokens_override: int | None = None,
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
    if max_tokens_override is not None:
        effective_max_tokens: int | None = max_tokens_override
    elif config.max_tokens is not None:
        effective_max_tokens = config.max_tokens
    else:
        effective_max_tokens = default_max_tokens_for(model)
    # Gate `cache_control_breakpoints` on `model.supports_prompt_cache`.
    # The breakpoints are an Anthropic-specific cache marker; sending
    # them to local providers (Ollama, llama.cpp, vLLM) is at best
    # ignored and at worst breaks `--jinja` template rendering when
    # an unexpected `cache_control` key lands on a user/assistant block.
    # When caching isn't supported we still cut the system prompt at
    # natural boundaries — just without the explicit Anthropic markers.
    cache_breakpoints = config.cache_control_breakpoints if model.supports_prompt_cache else 0
    ctx = Context(
        model=model,
        api=api,
        system=system,
        messages=list(messages),
        tools=list(tools),
        temperature=config.temperature,
        max_tokens=effective_max_tokens,
        thinking=config.thinking,
        cache_control_breakpoints=cache_breakpoints,
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
        joined = "".join(text_parts)
        visible, leaked = _split_thinking_tags(joined)
        if leaked:
            # Leaked thinking goes into the ThinkingBlock for accountability;
            # if no thinking block existed yet, synthesise one.
            existing = next(
                (b for b in content if isinstance(b, ThinkingBlock)),
                None,
            )
            if existing is None:
                content.append(ThinkingBlock(thinking=leaked, signature=None))
            else:
                # Append leaked thinking to the existing block's text.
                merged = (existing.thinking + "\n" + leaked).strip()
                idx = content.index(existing)
                content[idx] = ThinkingBlock(thinking=merged, signature=existing.signature)
        if visible:
            content.append(TextContent(text=visible))
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
