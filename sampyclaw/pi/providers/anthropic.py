"""Anthropic native SSE wrapper with cache_control + thinking support.

Mirrors `anthropic-cache-control-payload.ts` + `anthropic-family-cache-
semantics.ts` + `anthropic-family-tool-payload-compat.ts` from openclaw
`pi-embedded-runner/`.

Cache breakpoints (4 max per Anthropic spec):
- system block (always)
- tools block (if tools present)
- last user turn (always when caching enabled)
- last assistant turn (only when conversation has 6+ turns)

The wrapper converts `Context` → Anthropic Messages payload, opens a
streaming POST to `/v1/messages`, and translates Anthropic's
content_block_delta SSE into the pi event union.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from sampyclaw.pi.messages import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ThinkingBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from sampyclaw.pi.models import Context
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
    register_provider_stream,
)
from sampyclaw.pi.thinking import ANTHROPIC_THINKING_BUDGETS, ThinkingLevel

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

ANTHROPIC_VERSION = "2023-06-01"
# Beta header turns on prompt caching + 1M context for Sonnet 4.6.
ANTHROPIC_BETA = "prompt-caching-2024-07-31,context-1m-2025-08-07"


# ─── Payload shaping ─────────────────────────────────────────────────


def _serialize_user_content(content: Any) -> Any:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageContent):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": block.media_type,
                        "data": block.data,
                    },
                }
            )
    return out


def _serialize_assistant_content(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif isinstance(block, ThinkingBlock):
            entry: dict[str, Any] = {"type": "thinking", "thinking": block.thinking}
            if block.signature:
                entry["signature"] = block.signature
            out.append(entry)
    return out


def _serialize_tool_result(msg: ToolResultMessage) -> dict[str, Any]:
    """Anthropic emits tool results as a user message with tool_result blocks."""
    blocks: list[dict[str, Any]] = []
    for r in msg.results:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": r.tool_use_id,
        }
        if isinstance(r.content, str):
            block["content"] = r.content
        else:
            block["content"] = [
                {"type": "text", "text": b.text}
                if isinstance(b, TextContent)
                else {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": b.media_type,
                        "data": b.data,
                    },
                }
                for b in r.content
            ]
        if r.is_error:
            block["is_error"] = True
        blocks.append(block)
    return {"role": "user", "content": blocks}


def _serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            # System lifted to top-level field; skip from messages list.
            continue
        if isinstance(msg, UserMessage):
            out.append({"role": "user", "content": _serialize_user_content(msg.content)})
        elif isinstance(msg, AssistantMessage):
            out.append(
                {
                    "role": "assistant",
                    "content": _serialize_assistant_content(msg.content),
                }
            )
        elif isinstance(msg, ToolResultMessage):
            out.append(_serialize_tool_result(msg))
        elif isinstance(msg, dict):
            out.append(msg)
    return out


def _serialize_tools(tools: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict):
            out.append(t)
            continue
        out.append(
            {
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "input_schema": getattr(t, "input_schema", {}) or {"type": "object"},
            }
        )
    return out


def _apply_cache_control(
    payload: dict[str, Any], *, breakpoints: int
) -> dict[str, Any]:
    """Place `cache_control: {type: "ephemeral"}` markers up to `breakpoints`
    times. Order: system → tools → last user → last assistant. Anthropic
    accepts at most 4 markers."""
    if breakpoints <= 0:
        return payload
    placed = 0

    system = payload.get("system")
    if isinstance(system, list) and system and placed < breakpoints:
        system[-1]["cache_control"] = {"type": "ephemeral"}
        placed += 1
    elif isinstance(system, str) and system and placed < breakpoints:
        payload["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        placed += 1

    tools = payload.get("tools")
    if isinstance(tools, list) and tools and placed < breakpoints:
        tools[-1]["cache_control"] = {"type": "ephemeral"}
        placed += 1

    messages = payload.get("messages") or []
    # Last user message
    for msg in reversed(messages):
        if msg.get("role") == "user" and placed < breakpoints:
            content = msg.get("content")
            if isinstance(content, list) and content:
                content[-1]["cache_control"] = {"type": "ephemeral"}
                placed += 1
            break
    # Last assistant message — only if conversation is long enough that
    # the marker buys repeated reads.
    if len(messages) >= 6 and placed < breakpoints:
        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list) and content:
                    content[-1]["cache_control"] = {"type": "ephemeral"}
                    placed += 1
                break
    return payload


def build_anthropic_payload(ctx: Context) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": ctx.model.id,
        "messages": _serialize_messages(ctx.messages),
        "max_tokens": ctx.max_tokens or ctx.model.max_output_tokens,
    }
    if ctx.system:
        payload["system"] = ctx.system
    if ctx.temperature is not None:
        payload["temperature"] = ctx.temperature
    if ctx.tools:
        payload["tools"] = _serialize_tools(ctx.tools)
    if ctx.stop_sequences:
        payload["stop_sequences"] = list(ctx.stop_sequences)
    if ctx.thinking:
        # `ctx.thinking` may be a literal Anthropic dict or a ThinkingLevel
        # enum value; resolve the budget either way.
        if isinstance(ctx.thinking, dict):
            payload["thinking"] = ctx.thinking
        else:
            level = ThinkingLevel(ctx.thinking)
            budget = ANTHROPIC_THINKING_BUDGETS.get(level, 0)
            if budget > 0:
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if ctx.model.supports_prompt_cache and ctx.cache_control_breakpoints > 0:
        payload = _apply_cache_control(
            payload, breakpoints=min(4, ctx.cache_control_breakpoints)
        )
    return payload


# ─── SSE consumption ─────────────────────────────────────────────────


async def stream_anthropic(
    ctx: Context, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    payload = build_anthropic_payload(ctx)
    if opts.extra_params:
        payload.update(opts.extra_params)
    payload["stream"] = True

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ANTHROPIC_BETA,
        "Accept": "text/event-stream",
    }
    if ctx.api.api_key:
        headers["x-api-key"] = ctx.api.api_key
    headers.update(ctx.api.extra_headers)
    url = ctx.api.base_url.rstrip("/") + "/v1/messages"

    timeout = aiohttp.ClientTimeout(total=opts.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    yield ErrorEvent(
                        message=f"HTTP {resp.status}: {body[:300]}",
                        retryable=resp.status in RETRYABLE_STATUS,
                    )
                    return

                # Per-block accumulators keyed by the `index` Anthropic emits.
                block_kind: dict[int, str] = {}
                tool_meta: dict[int, dict[str, str]] = {}
                stop_emitted = False

                async for raw in resp.content:
                    if opts.abort_event is not None and opts.abort_event.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "content_block_start":
                        idx = event.get("index", 0)
                        block = event.get("content_block") or {}
                        bt = block.get("type", "")
                        block_kind[idx] = bt
                        if bt == "tool_use":
                            tool_meta[idx] = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                            }
                            yield ToolUseStartEvent(
                                id=tool_meta[idx]["id"], name=tool_meta[idx]["name"]
                            )
                    elif etype == "content_block_delta":
                        idx = event.get("index", 0)
                        delta = event.get("delta") or {}
                        dt = delta.get("type")
                        if dt == "text_delta":
                            yield TextDeltaEvent(delta=delta.get("text", ""))
                        elif dt == "thinking_delta":
                            yield ThinkingDeltaEvent(
                                delta=delta.get("thinking", "")
                            )
                        elif dt == "signature_delta":
                            # Final signature for the thinking block. Carry on the
                            # next thinking_delta as `signature=` is closed.
                            yield ThinkingDeltaEvent(
                                delta="", signature=delta.get("signature")
                            )
                        elif dt == "input_json_delta":
                            tid = tool_meta.get(idx, {}).get("id", "")
                            yield ToolUseInputDeltaEvent(
                                id=tid, input_delta=delta.get("partial_json", "")
                            )
                    elif etype == "content_block_stop":
                        idx = event.get("index", 0)
                        if block_kind.get(idx) == "tool_use":
                            yield ToolUseEndEvent(id=tool_meta[idx]["id"])
                    elif etype == "message_delta":
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            yield UsageEvent(usage=usage)
                        delta = event.get("delta") or {}
                        stop_reason = delta.get("stop_reason")
                        if stop_reason:
                            yield StopEvent(reason=stop_reason)
                            stop_emitted = True
                    elif etype == "message_stop":
                        if not stop_emitted:
                            yield StopEvent(reason="end_turn")
                            stop_emitted = True
                if not stop_emitted:
                    yield StopEvent(reason="end_turn")
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
            yield ErrorEvent(
                message=f"connection error: {exc}", retryable=True, error=exc
            )


register_provider_stream("anthropic", stream_anthropic)
register_provider_stream("anthropic-vertex", stream_anthropic)


__all__ = [
    "build_anthropic_payload",
    "stream_anthropic",
]
