"""AWS Bedrock invoke-model wrapper.

Mirrors `bedrock-stream-wrappers.ts` — Bedrock hosts Anthropic and a few
other model families. For Anthropic-on-Bedrock the payload is identical to
the native Anthropic API except:
- No `model` field in the body (model id is in the URL).
- `anthropic_version` field instead of header.
- Streaming via `/model/{id}/invoke-with-response-stream`.
- AWS SigV4 auth — for now we punt and require a SIGV4-presigned URL or
  an HTTP proxy that adds the signature (the gateway operator's choice).
  The wrapper accepts a pre-signed URL via `model.extra["presigned_url"]`.

Models with `is_anthropic_bedrock_model(id)` route through anthropic
payload semantics; others fall through to the OpenAI-shape wrapper for
Bedrock-hosted Llama / Mistral / Titan via Bedrock's `converse` API,
which is OpenAI-shape compatible.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import aiohttp

from oxenclaw.pi.models import Context
from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.providers.anthropic import (
    RETRYABLE_STATUS,
    build_anthropic_payload,
)
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
    register_provider_stream,
)


def is_anthropic_bedrock_model(model_id: str) -> bool:
    """Anthropic-hosted-on-Bedrock model id detection.

    Bedrock prefixes Anthropic ids with `anthropic.` — we treat any id
    starting with that prefix as Anthropic-family for payload purposes."""
    return model_id.startswith("anthropic.") or "claude" in model_id.lower()


async def stream_bedrock(
    ctx: Context, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    if not is_anthropic_bedrock_model(ctx.model.id):
        # Non-Anthropic Bedrock model — assume the operator is fronting it
        # with a converse-API → OpenAI-shape proxy.
        async for ev in stream_openai_compatible(ctx, opts):
            yield ev
        return

    payload = build_anthropic_payload(ctx)
    payload.pop("model", None)
    payload["anthropic_version"] = "bedrock-2023-05-31"
    if opts.extra_params:
        payload.update(opts.extra_params)

    base = ctx.api.base_url.rstrip("/")
    presigned = (
        ctx.model.extra.get("presigned_url")
        if isinstance(ctx.model.extra.get("presigned_url"), str)
        else None
    )
    url = presigned or (f"{base}/model/{ctx.model.id}/invoke-with-response-stream")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(ctx.api.extra_headers)

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
                # Bedrock event-stream is application/vnd.amazon.eventstream;
                # we expect the gateway to be a proxy that re-emits as
                # text/event-stream JSON-line frames matching Anthropic. The
                # parsing path mirrors anthropic.stream_anthropic.
                stop_emitted = False
                tool_meta: dict[int, dict[str, str]] = {}
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
                        if block.get("type") == "tool_use":
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
                            yield ThinkingDeltaEvent(delta=delta.get("thinking", ""))
                        elif dt == "input_json_delta":
                            tid = tool_meta.get(idx, {}).get("id", "")
                            yield ToolUseInputDeltaEvent(
                                id=tid, input_delta=delta.get("partial_json", "")
                            )
                    elif etype == "content_block_stop":
                        idx = event.get("index", 0)
                        if idx in tool_meta:
                            yield ToolUseEndEvent(id=tool_meta[idx]["id"])
                    elif etype == "message_delta":
                        usage = event.get("usage")
                        if isinstance(usage, dict):
                            yield UsageEvent(usage=usage)
                        delta = event.get("delta") or {}
                        if delta.get("stop_reason"):
                            yield StopEvent(reason=delta["stop_reason"])
                            stop_emitted = True
                    elif etype == "message_stop":
                        if not stop_emitted:
                            yield StopEvent(reason="end_turn")
                            stop_emitted = True
                if not stop_emitted:
                    yield StopEvent(reason="end_turn")
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            yield ErrorEvent(message=f"connection error: {exc}", retryable=True, error=exc)


register_provider_stream("bedrock", stream_bedrock)


__all__ = ["is_anthropic_bedrock_model", "stream_bedrock"]
