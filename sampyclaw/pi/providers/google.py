"""Google Gemini `streamGenerateContent` SSE wrapper.

Mirrors `google-stream-wrappers.ts` + `google-prompt-cache.ts`. Gemini's
shape differs significantly from OpenAI/Anthropic:
- Roles: `user` and `model` (assistant). System lifts to `systemInstruction`.
- Content: `parts: [{text}, {inlineData:{mimeType,data}}, {functionCall},
  {functionResponse}, {thought}]`.
- Tools: `tools: [{functionDeclarations: [...]}]`.
- Stream endpoint: `:streamGenerateContent?alt=sse`.
- Thinking: `generationConfig.thinkingConfig.thinkingBudget` (Pro+).
"""

from __future__ import annotations

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
from sampyclaw.pi.thinking import GEMINI_THINKING_BUDGETS, ThinkingLevel

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})


def _serialize_user_parts(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            out.append({"text": block.text})
        elif isinstance(block, ImageContent):
            out.append({"inlineData": {"mimeType": block.media_type, "data": block.data}})
    return out


def _serialize_assistant_parts(content: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextContent):
            out.append({"text": block.text})
        elif isinstance(block, ToolUseBlock):
            out.append({"functionCall": {"name": block.name, "args": block.input}})
        elif isinstance(block, ThinkingBlock):
            out.append({"thought": True, "text": block.thinking})
    return out


def _serialize_tool_response(msg: ToolResultMessage) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for r in msg.results:
        # Gemini wants the response as JSON object under `response`.
        if isinstance(r.content, str):
            try:
                response_obj: Any = json.loads(r.content)
            except json.JSONDecodeError:
                response_obj = {"output": r.content}
            if not isinstance(response_obj, dict):
                response_obj = {"output": response_obj}
        else:
            response_obj = {
                "output": [
                    {"text": b.text} if isinstance(b, TextContent) else None
                    for b in r.content
                    if isinstance(b, TextContent)
                ]
            }
        parts.append(
            {
                "functionResponse": {
                    "name": r.tool_use_id,  # Gemini matches by name; we use id-as-name
                    "response": response_obj,
                }
            }
        )
    return {"role": "user", "parts": parts}


def _serialize_messages(messages: list[Any]) -> tuple[str | None, list[dict[str, Any]]]:
    system: str | None = None
    out: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            # Gemini prefers a single systemInstruction; concatenate if multiple.
            system = (system + "\n\n" + msg.content) if system else msg.content
        elif isinstance(msg, UserMessage):
            out.append({"role": "user", "parts": _serialize_user_parts(msg.content)})
        elif isinstance(msg, AssistantMessage):
            out.append({"role": "model", "parts": _serialize_assistant_parts(msg.content)})
        elif isinstance(msg, ToolResultMessage):
            out.append(_serialize_tool_response(msg))
        elif isinstance(msg, dict):
            out.append(msg)
    return system, out


def _serialize_tools(tools: list[Any]) -> list[dict[str, Any]]:
    decls: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, dict):
            decls.append(t)
            continue
        decls.append(
            {
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "parameters": getattr(t, "input_schema", {}) or {"type": "object"},
            }
        )
    return [{"functionDeclarations": decls}] if decls else []


def build_google_payload(ctx: Context) -> tuple[dict[str, Any], str | None]:
    system, contents = _serialize_messages(ctx.messages)
    if ctx.system:
        system = ctx.system if not system else f"{ctx.system}\n\n{system}"

    gen_config: dict[str, Any] = {}
    if ctx.temperature is not None:
        gen_config["temperature"] = ctx.temperature
    if ctx.max_tokens is not None:
        gen_config["maxOutputTokens"] = ctx.max_tokens
    if ctx.stop_sequences:
        gen_config["stopSequences"] = list(ctx.stop_sequences)

    if ctx.thinking and ctx.model.supports_thinking:
        if isinstance(ctx.thinking, dict):
            gen_config["thinkingConfig"] = ctx.thinking
        else:
            level = ThinkingLevel(ctx.thinking)
            budget = GEMINI_THINKING_BUDGETS.get(level, 0)
            if budget > 0:
                gen_config["thinkingConfig"] = {"thinkingBudget": budget}

    payload: dict[str, Any] = {"contents": contents}
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if gen_config:
        payload["generationConfig"] = gen_config
    if ctx.tools:
        payload["tools"] = _serialize_tools(ctx.tools)
    return payload, system


async def stream_google(
    ctx: Context, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    payload, _ = build_google_payload(ctx)
    if opts.extra_params:
        payload.update(opts.extra_params)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    headers.update(ctx.api.extra_headers)
    base = ctx.api.base_url.rstrip("/")
    url = f"{base}/v1beta/models/{ctx.model.id}:streamGenerateContent?alt=sse"
    if ctx.api.api_key:
        url += f"&key={ctx.api.api_key}"

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

                # Tool calls in Gemini arrive as fully-formed functionCall
                # parts (not deltas). We synthesise start/inputDelta/end from
                # one chunk so downstream loop logic is uniform.
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

                    candidates = event.get("candidates") or []
                    if not candidates:
                        usage = event.get("usageMetadata")
                        if isinstance(usage, dict):
                            yield UsageEvent(usage=usage)
                        continue
                    cand = candidates[0]
                    parts = (cand.get("content") or {}).get("parts") or []
                    for part in parts:
                        if "text" in part and not part.get("thought"):
                            yield TextDeltaEvent(delta=part["text"])
                        elif part.get("thought") and "text" in part:
                            yield ThinkingDeltaEvent(delta=part["text"])
                        elif "functionCall" in part:
                            fc = part["functionCall"]
                            tid = fc.get("name", "") + "-call"
                            yield ToolUseStartEvent(id=tid, name=fc.get("name", ""))
                            yield ToolUseInputDeltaEvent(
                                id=tid,
                                input_delta=json.dumps(fc.get("args") or {}, ensure_ascii=False),
                            )
                            yield ToolUseEndEvent(id=tid)

                    finish = cand.get("finishReason")
                    if finish:
                        yield StopEvent(reason=finish.lower())
                        stop_emitted = True

                    usage = event.get("usageMetadata")
                    if isinstance(usage, dict):
                        yield UsageEvent(usage=usage)
                if not stop_emitted:
                    yield StopEvent(reason="end_turn")
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            yield ErrorEvent(message=f"connection error: {exc}", retryable=True, error=exc)


register_provider_stream("google", stream_google)
register_provider_stream("vertex-ai", stream_google)


__all__ = ["build_google_payload", "stream_google"]
