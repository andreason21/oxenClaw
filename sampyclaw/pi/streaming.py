"""Streaming primitives — `StreamFn`, event types, helper builders.

The pi-* TS shape is roughly:

  type StreamFn = (ctx: Context, opts: SimpleStreamOptions) =>
      AsyncIterable<AssistantMessageEvent>

  type AssistantMessageEvent =
    | { kind: "text_delta", delta: string }
    | { kind: "thinking_delta", delta: string, signature?: string }
    | { kind: "tool_use_start", id: string, name: string }
    | { kind: "tool_use_input_delta", id: string, input_delta: string }
    | { kind: "tool_use_end", id: string }
    | { kind: "usage", usage: {...} }
    | { kind: "stop", reason: string }
    | { kind: "error", error: Error }

  function streamSimple(ctx, opts): AsyncIterable<AssistantMessageEvent>
  function createAssistantMessageEventStream(): {push, end, iterate}

These let the run loop consume token deltas uniformly across providers —
each provider's wrapper translates its native SSE shape into this event
union, and the loop assembles a final `AssistantMessage` from the events.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Union


# ─── Event types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TextDeltaEvent:
    kind: Literal["text_delta"] = "text_delta"
    delta: str = ""


@dataclass(frozen=True)
class ThinkingDeltaEvent:
    kind: Literal["thinking_delta"] = "thinking_delta"
    delta: str = ""
    signature: str | None = None


@dataclass(frozen=True)
class ToolUseStartEvent:
    kind: Literal["tool_use_start"] = "tool_use_start"
    id: str = ""
    name: str = ""


@dataclass(frozen=True)
class ToolUseInputDeltaEvent:
    """Streaming JSON arguments. `input_delta` is a *fragment* — the loop
    concatenates fragments per id then JSON-parses at end-of-tool."""

    kind: Literal["tool_use_input_delta"] = "tool_use_input_delta"
    id: str = ""
    input_delta: str = ""


@dataclass(frozen=True)
class ToolUseEndEvent:
    kind: Literal["tool_use_end"] = "tool_use_end"
    id: str = ""


@dataclass(frozen=True)
class UsageEvent:
    """Token usage report; usually arrives once per stream near the end."""

    kind: Literal["usage"] = "usage"
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StopEvent:
    kind: Literal["stop"] = "stop"
    reason: str = "end_turn"


@dataclass(frozen=True)
class ErrorEvent:
    kind: Literal["error"] = "error"
    error: BaseException | None = None
    message: str = ""
    retryable: bool = False


AssistantMessageEvent = Union[
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolUseStartEvent,
    ToolUseInputDeltaEvent,
    ToolUseEndEvent,
    UsageEvent,
    StopEvent,
    ErrorEvent,
]


# ─── SimpleStreamOptions ──────────────────────────────────────────────


@dataclass(frozen=True)
class SimpleStreamOptions:
    """Knobs handed to a provider stream wrapper."""

    abort_event: asyncio.Event | None = None
    timeout_seconds: float | None = None
    # Used by the openai-compatible wrapper to flip on `stream_options.
    # include_usage`; harmless on providers that ignore it.
    include_usage: bool = True
    # Provider-specific extras (e.g. OpenAI `reasoning_effort`).
    extra_params: dict[str, Any] = field(default_factory=dict)


# ─── StreamFn protocol ────────────────────────────────────────────────


class StreamFn(Protocol):
    """Provider stream entry point. Implemented by each provider wrapper."""

    def __call__(
        self, ctx: Any, opts: SimpleStreamOptions
    ) -> AsyncIterator[AssistantMessageEvent]: ...


# ─── Push-based event stream helper ───────────────────────────────────


class AssistantMessageEventStream:
    """Producer/consumer queue used when a wrapper wants imperative push
    instead of `yield`. Mirrors `createAssistantMessageEventStream()` in
    pi-ai."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[AssistantMessageEvent | None] = asyncio.Queue()
        self._closed = False

    async def push(self, event: AssistantMessageEvent) -> None:
        if self._closed:
            return
        await self._queue.put(event)

    async def end(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def iterate(self) -> AsyncIterator[AssistantMessageEvent]:
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev


def create_assistant_message_event_stream() -> AssistantMessageEventStream:
    """Equivalent of `createAssistantMessageEventStream()` in pi-ai."""
    return AssistantMessageEventStream()


# ─── streamSimple — runtime-side dispatch ─────────────────────────────


# Lazy registry so providers can register themselves at import time without
# pulling them all in unconditionally.
_PROVIDER_STREAMS: dict[str, "StreamFnImpl"] = {}

StreamFnImpl = Callable[[Any, SimpleStreamOptions], AsyncIterator[AssistantMessageEvent]]


def register_provider_stream(provider_id: str, fn: StreamFnImpl) -> None:
    _PROVIDER_STREAMS[provider_id] = fn


def get_provider_stream(provider_id: str) -> StreamFnImpl:
    if provider_id not in _PROVIDER_STREAMS:
        raise KeyError(
            f"no streaming wrapper registered for provider {provider_id!r}; "
            f"known: {sorted(_PROVIDER_STREAMS)}"
        )
    return _PROVIDER_STREAMS[provider_id]


def stream_simple(
    ctx: Any, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    """Dispatch to the registered provider stream wrapper.

    `ctx.model.provider` selects the wrapper. Wrappers register at import
    time via `register_provider_stream(...)`.
    """
    fn = get_provider_stream(ctx.model.provider)
    return fn(ctx, opts)


__all__ = [
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
    "ErrorEvent",
    "SimpleStreamOptions",
    "StopEvent",
    "StreamFn",
    "StreamFnImpl",
    "TextDeltaEvent",
    "ThinkingDeltaEvent",
    "ToolUseEndEvent",
    "ToolUseInputDeltaEvent",
    "ToolUseStartEvent",
    "UsageEvent",
    "create_assistant_message_event_stream",
    "get_provider_stream",
    "register_provider_stream",
    "stream_simple",
]
