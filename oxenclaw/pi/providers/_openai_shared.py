"""Shared OpenAI-compatible chat-completions SSE wrapper.

Every provider that exposes a `POST /v1/chat/completions` SSE endpoint
shares this code: openai, ollama, lmstudio, vllm, llamacpp, litellm,
openai-compatible, proxy, openrouter (with capabilities), moonshot, zai,
minimax. Provider-specific wrappers in sibling modules call into here
with their own `payload_patch` and `extra_params`.

The wrapper translates each SSE chunk to the pi event union:
- `delta.content` → `TextDeltaEvent`
- `delta.tool_calls[i]` → `ToolUseStart` (first delta with `id`+`name`)
                          + `ToolUseInputDelta` (each `arguments` fragment)
                          + `ToolUseEnd` (when finish_reason="tool_calls")
- `delta.reasoning` (o1/o3, deepseek-r1) → `ThinkingDeltaEvent`
- final `usage` (when `stream_options.include_usage=True`) → `UsageEvent`
- `finish_reason` → `StopEvent`
- network/HTTP error → `ErrorEvent(retryable=...)` + raise StopIteration
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

from oxenclaw.observability import llm_trace
from oxenclaw.pi.messages import (
    AssistantMessage,
    ImageContent,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from oxenclaw.pi.models import Context
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
)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

PayloadPatch = Callable[[dict[str, Any]], dict[str, Any]]


def _serialize_message(msg: Any) -> dict[str, Any]:
    """Translate a pi `AgentMessage` to one OpenAI chat message dict.

    Tool results from us become `{"role":"tool", "tool_call_id":..., "content":...}`
    entries (one per ToolResultBlock). The caller flattens.
    """
    if isinstance(msg, SystemMessage):
        return {"role": "system", "content": msg.content}
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            return {"role": "user", "content": msg.content}
        # Multi-block user (text + image). OpenAI uses `[{"type":"text",...},
        # {"type":"image_url","image_url":{"url":"data:..."}}]`.
        parts: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                parts.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{block.media_type};base64,{block.data}"},
                    }
                )
        return {"role": "user", "content": parts}
    if isinstance(msg, AssistantMessage):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append(
                    {
                        "id": block.id,
                        "type": "function",
                        "function": {
                            "name": block.name,
                            "arguments": json.dumps(block.input, ensure_ascii=False),
                        },
                    }
                )
            # ThinkingBlock: OpenAI does not echo thinking back; drop on send.
        out: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            out["content"] = "".join(text_parts)
        else:
            out["content"] = None
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out
    if isinstance(msg, ToolResultMessage):
        # The caller flattens this into N tool messages.
        raise _ToolResultExpand(msg)
    if hasattr(msg, "model_dump"):
        return msg.model_dump()
    if isinstance(msg, dict):
        return msg
    raise ValueError(f"unknown message shape: {type(msg).__name__}")


class _ToolResultExpand(Exception):
    def __init__(self, msg: ToolResultMessage) -> None:
        self.msg = msg


def _serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        try:
            out.append(_serialize_message(msg))
        except _ToolResultExpand as exp:
            for r in exp.msg.results:
                content = (
                    r.content
                    if isinstance(r.content, str)
                    else json.dumps(
                        [
                            {"type": "text", "text": b.text}
                            if isinstance(b, TextContent)
                            else {"type": "image_url", "image_url": {"url": "data:..."}}
                            for b in r.content
                        ]
                    )
                )
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": r.tool_use_id,
                        "content": content,
                    }
                )
    return out


def _serialize_tools(tools: list[Any]) -> list[dict[str, Any]]:
    """OpenAI tool format: `[{"type":"function", "function":{name, description,
    parameters}}, ...]`."""
    out: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict):
            out.append(t)
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": getattr(t, "name", ""),
                    "description": getattr(t, "description", ""),
                    "parameters": getattr(t, "input_schema", {}) or {},
                },
            }
        )
    return out


def build_openai_payload(ctx: Context, *, stream: bool) -> dict[str, Any]:
    """Pure helper — build the JSON payload OpenAI-shape providers expect."""
    messages = _serialize_messages(ctx.messages)
    if ctx.system:
        # Lift system into the messages list if not already present.
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": ctx.system})
    payload: dict[str, Any] = {
        "model": ctx.model.id,
        "messages": messages,
        "temperature": ctx.temperature,
        "stream": stream,
    }
    if ctx.max_tokens is not None:
        payload["max_tokens"] = ctx.max_tokens
        # Ollama OpenAI shim accepts num_predict; harmless on real OpenAI.
        if ctx.model.provider in ("ollama", "openai-compatible", "proxy"):
            payload["num_predict"] = ctx.max_tokens
    if ctx.tools:
        payload["tools"] = _serialize_tools(ctx.tools)
    if ctx.stop_sequences:
        payload["stop"] = list(ctx.stop_sequences)
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


async def stream_openai_compatible(
    ctx: Context,
    opts: SimpleStreamOptions,
    *,
    payload_patch: PayloadPatch | None = None,
    path: str = "/chat/completions",
) -> AsyncIterator[AssistantMessageEvent]:
    """OpenAI-shape SSE stream → AssistantMessageEvent iterator.

    `payload_patch` is a hook for provider-specific tweaks (extra params,
    field renames) that `streamWithPayloadPatch` provides on the TS side.
    """
    payload = build_openai_payload(ctx, stream=True)
    if opts.extra_params:
        payload.update(opts.extra_params)
    if payload_patch is not None:
        payload = payload_patch(payload)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if ctx.api.api_key:
        headers["Authorization"] = f"Bearer {ctx.api.api_key}"
    if ctx.api.organization:
        headers["OpenAI-Organization"] = ctx.api.organization
    headers.update(ctx.api.extra_headers)

    url = ctx.api.base_url.rstrip("/") + path

    # Wire-level trace (no-op unless OXENCLAW_LLM_TRACE=1). Captures the
    # *final* payload after every patch + the assembled response.
    trace_id = llm_trace.new_request_id()
    trace_provider = getattr(ctx.model, "provider", "openai-compatible") or "openai-compatible"
    trace_model = getattr(ctx.model, "id", "?")
    trace_t0 = time.monotonic()
    llm_trace.log_request(
        request_id=trace_id,
        provider=trace_provider,
        model_id=trace_model,
        url=url,
        payload=payload,
    )
    # Aggregators for the `response` event.
    _trace_text_parts: list[str] = []
    _trace_tool_calls: dict[int, dict[str, Any]] = {}
    _trace_finish_reason: str | None = None
    _trace_usage: dict[str, Any] | None = None

    # Per-stream session — runner can pool externally if needed.
    timeout = aiohttp.ClientTimeout(total=opts.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    llm_trace.log_error(
                        request_id=trace_id,
                        provider=trace_provider,
                        model_id=trace_model,
                        status=resp.status,
                        message=body,
                        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
                    )
                    yield ErrorEvent(
                        message=f"HTTP {resp.status}: {body[:300]}",
                        retryable=resp.status in RETRYABLE_STATUS,
                    )
                    return

                # tool_calls are sent as deltas keyed by `index`.
                tool_buf: dict[int, dict[str, str]] = {}
                tool_started: set[int] = set()
                emitted_stop = False

                async for raw in resp.content:
                    if opts.abort_event is not None and opts.abort_event.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    usage = event.get("usage")
                    if isinstance(usage, dict):
                        _trace_usage = usage
                        yield UsageEvent(usage=usage)

                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    # Reasoning / thinking deltas (OpenAI o-series, deepseek-r1).
                    reasoning = delta.get("reasoning") or delta.get("thinking")
                    if isinstance(reasoning, str) and reasoning:
                        yield ThinkingDeltaEvent(delta=reasoning)

                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        _trace_text_parts.append(content)
                        yield TextDeltaEvent(delta=content)

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        trace_slot = _trace_tool_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                            trace_slot["id"] = tc["id"]
                        fn_delta = tc.get("function") or {}
                        if fn_delta.get("name"):
                            slot["name"] += fn_delta["name"]
                            trace_slot["name"] = (trace_slot.get("name") or "") + fn_delta["name"]
                        if fn_delta.get("arguments"):
                            slot["arguments"] += fn_delta["arguments"]
                            trace_slot["arguments"] = (
                                trace_slot.get("arguments") or ""
                            ) + fn_delta["arguments"]
                        if idx not in tool_started and slot["id"] and slot["name"]:
                            tool_started.add(idx)
                            yield ToolUseStartEvent(id=slot["id"], name=slot["name"])
                        if idx in tool_started and fn_delta.get("arguments"):
                            yield ToolUseInputDeltaEvent(
                                id=slot["id"], input_delta=fn_delta["arguments"]
                            )

                    finish = choice.get("finish_reason")
                    if finish:
                        _trace_finish_reason = finish
                        # Close any open tool_use blocks before stop.
                        for idx in sorted(tool_started):
                            yield ToolUseEndEvent(id=tool_buf[idx]["id"])
                        tool_started.clear()
                        yield StopEvent(reason=finish)
                        emitted_stop = True
                if not emitted_stop:
                    for idx in sorted(tool_started):
                        yield ToolUseEndEvent(id=tool_buf[idx]["id"])
                    yield StopEvent(reason="end_turn")
                    if _trace_finish_reason is None:
                        _trace_finish_reason = "end_turn"
            llm_trace.log_response(
                request_id=trace_id,
                provider=trace_provider,
                model_id=trace_model,
                content="".join(_trace_text_parts),
                tool_calls=[_trace_tool_calls[i] for i in sorted(_trace_tool_calls)],
                finish_reason=_trace_finish_reason,
                usage=_trace_usage,
                duration_ms=(time.monotonic() - trace_t0) * 1000.0,
            )
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            llm_trace.log_error(
                request_id=trace_id,
                provider=trace_provider,
                model_id=trace_model,
                status=None,
                message=f"connection error: {exc}",
                duration_ms=(time.monotonic() - trace_t0) * 1000.0,
            )
            yield ErrorEvent(message=f"connection error: {exc}", retryable=True, error=exc)


__all__ = [
    "PayloadPatch",
    "build_openai_payload",
    "stream_openai_compatible",
]
