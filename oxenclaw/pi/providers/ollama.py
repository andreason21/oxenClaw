"""Ollama native `/api/chat` provider.

We bypass Ollama's OpenAI compatibility shim because that shim does not
honor `options.num_ctx` — it silently truncates large prompts to the
server default (typically 4096). With memory + skill manifests, oxenclaw
hits multi-tens-of-KB system prompts; truncation drops the tool schemas
and the whole tool-call protocol falls apart.

The native endpoint exposes the full `options` surface (`num_ctx`,
`num_predict`, `temperature`, `stop`, `seed`, ...), accepts the same
OpenAI-shape `tools` array, and returns `tool_calls` reliably. Tool
calls in native are emitted as `{"function":{"name", "arguments":{...}}}`
with `arguments` already a JSON object (not a string) and no `id`
field; we synthesise an `id` so downstream tool-runner correlation
still works.

Activation:
- `OXENCLAW_OLLAMA_NUM_CTX=N`   — override default (16384)
- Provider id `ollama` is registered here at import time.
"""

from __future__ import annotations

import json
import os
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
    register_provider_stream,
)

PayloadPatch = Callable[[dict[str, Any]], dict[str, Any]]
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})

# Default num_ctx the native provider sends. Picked to fit memory + skill
# manifest blobs on a 16 GB-class GPU/CPU machine. Override via
# `OXENCLAW_OLLAMA_NUM_CTX=N` (raw int) or `OXENCLAW_OLLAMA_NUM_CTX=auto`
# to detect from `/api/show` (capped at `_NUM_CTX_AUTO_CAP`).
#
# `_NUM_CTX_AUTO_CAP` is intentionally equal to the default: auto only
# *lowers* num_ctx for models whose max is below the default (rare),
# never raises it. Operators with headroom should set an explicit
# integer — auto must not hurt anyone who flips it on. A bigger cap
# previously locked up 16 GB machines for ~5 minutes during cold KV
# allocation; even when it eventually loaded, concurrent embedding
# requests against the same Ollama server timed out.
_DEFAULT_NUM_CTX = 32768
_NUM_CTX_AUTO_CAP = 32768

# Per-process cache of the resolved num_ctx, keyed by model id. Avoids
# hitting `/api/show` once per request when the user opts into auto mode.
_resolved_ctx_cache: dict[str, int] = {}


def _native_base_url(api_base: str) -> str:
    """Strip the `/v1` suffix the OpenAI shim uses so we can hit
    `/api/chat` regardless of how the caller configured the API."""
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base


async def _detect_model_max_ctx(
    base_url: str, model_id: str, *, timeout_s: float = 5.0
) -> dict[str, Any] | None:
    """Return the `model_info` block from `/api/show`, or None on failure.

    Caller pulls `*.context_length` and the GQA shape fields from the
    returned dict. Failures are swallowed: any HTTP / parse error means
    we silently fall back to `_DEFAULT_NUM_CTX`.
    """
    url = base_url + "/api/show"
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"name": model_id}) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json()
        info = data.get("model_info")
        return info if isinstance(info, dict) else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


def _is_text_model_key(k: str) -> bool:
    """Filter out vision-encoder fields when reading multimodal model_info.

    Multimodal models (e.g. qwen3.5:9b) expose both a language-model
    block (`qwen35.context_length`) and a vision-encoder block
    (`qwen35.vision.context_length`); the KV cache cost we care about
    is the LM side, so anything segmented under `.vision.` is excluded.
    """
    return ".vision." not in k


def _pick_field(info: dict[str, Any], suffix: str) -> int | None:
    for k, v in info.items():
        if (
            isinstance(k, str)
            and _is_text_model_key(k)
            and k.endswith(suffix)
            and isinstance(v, int)
        ):
            return v
    return None


def _pick_context_length(info: dict[str, Any]) -> int | None:
    return _pick_field(info, ".context_length")


def _estimate_kv_cache_gb(info: dict[str, Any], num_ctx: int) -> float | None:
    """Best-effort KV cache size estimate, in GiB, for the given num_ctx.

    Reads the LM-side model_info fields Ollama exposes. Returns None
    when the architecture-specific fields aren't present so the caller
    can skip the warning rather than print a wrong number.

    Refinements over the textbook MHA formula:
    - `attention.key_length` / `.value_length` override `embed / head`
      when present — Qwen3 / Mamba-hybrid models advertise these.
    - `full_attention_interval > 1` (SSM hybrids) means only every Nth
      block stores KV; the rest are recurrent state.
    - GQA (`head_count_kv < head_count`) is honoured.
    """
    block = _pick_field(info, ".block_count")
    embed = _pick_field(info, ".embedding_length")
    head = _pick_field(info, ".attention.head_count")
    if not (block and embed and head):
        return None
    head_kv = _pick_field(info, ".attention.head_count_kv") or head
    key_len = _pick_field(info, ".attention.key_length") or (embed // head)
    val_len = _pick_field(info, ".attention.value_length") or key_len
    interval = _pick_field(info, ".full_attention_interval") or 1
    attn_blocks = max(1, block // max(1, interval))
    bytes_per_value = 2  # assume FP16 KV cache
    kv_bytes = num_ctx * attn_blocks * head_kv * (key_len + val_len) * bytes_per_value
    return kv_bytes / (1024**3)


async def _resolve_num_ctx(base_url: str, model_id: str) -> int:
    """Resolve the num_ctx for the next request.

    `OXENCLAW_OLLAMA_NUM_CTX` accepts:
      - unset           → `_DEFAULT_NUM_CTX`
      - integer         → that value (raw, no cap — operator's call)
      - "auto"          → query `/api/show`, use min(model_max, _NUM_CTX_AUTO_CAP)
    """
    raw = os.environ.get("OXENCLAW_OLLAMA_NUM_CTX", "").strip()
    if not raw:
        return _DEFAULT_NUM_CTX
    if raw.lower() == "auto":
        cached = _resolved_ctx_cache.get(model_id)
        if cached is not None:
            return cached
        info = await _detect_model_max_ctx(base_url, model_id)
        if info is None:
            _resolved_ctx_cache[model_id] = _DEFAULT_NUM_CTX
            return _DEFAULT_NUM_CTX
        detected = _pick_context_length(info)
        resolved = _DEFAULT_NUM_CTX if detected is None else min(detected, _NUM_CTX_AUTO_CAP)
        _resolved_ctx_cache[model_id] = resolved
        kv_gb = _estimate_kv_cache_gb(info, resolved)
        kv_note = f" (~{kv_gb:.1f} GiB KV cache)" if kv_gb else ""
        logger = __import__("oxenclaw.plugin_sdk.runtime_env", fromlist=["get_logger"]).get_logger(
            "pi.ollama"
        )
        logger.info(
            "ollama %s: num_ctx=auto resolved to %d%s",
            model_id,
            resolved,
            kv_note,
        )
        return resolved
    try:
        return max(1024, int(raw))
    except ValueError:
        return _DEFAULT_NUM_CTX


def _serialize_message(msg: Any) -> list[dict[str, Any]]:
    """Translate a pi message to one or more native Ollama messages."""
    if isinstance(msg, SystemMessage):
        return [{"role": "system", "content": msg.content}]
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            return [{"role": "user", "content": msg.content}]
        text_parts: list[str] = []
        images: list[str] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                text_parts.append(block.text)
            elif isinstance(block, ImageContent):
                images.append(block.data)
        out: dict[str, Any] = {"role": "user", "content": "\n".join(text_parts)}
        if images:
            out["images"] = images
        return [out]
    if isinstance(msg, AssistantMessage):
        text_parts = []
        tool_calls: list[dict[str, Any]] = []
        for block in msg.content:
            if isinstance(block, TextContent):
                text_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_calls.append({"function": {"name": block.name, "arguments": block.input}})
        out = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return [out]
    if isinstance(msg, ToolResultMessage):
        out_msgs: list[dict[str, Any]] = []
        for r in msg.results:
            if isinstance(r.content, str):
                content = r.content
            else:
                content = json.dumps(
                    [
                        {"type": "text", "text": b.text}
                        if isinstance(b, TextContent)
                        else {"type": "image_url", "image_url": {"url": "data:..."}}
                        for b in r.content
                    ],
                    ensure_ascii=False,
                )
            out_msgs.append({"role": "tool", "content": content})
        return out_msgs
    if isinstance(msg, dict):
        return [msg]
    if hasattr(msg, "model_dump"):
        return [msg.model_dump()]
    raise ValueError(f"unknown message shape: {type(msg).__name__}")


def _serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        out.extend(_serialize_message(msg))
    return out


def _serialize_tools(tools: list[Any]) -> list[dict[str, Any]]:
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


def build_ollama_payload(ctx: Context, *, stream: bool, num_ctx: int) -> dict[str, Any]:
    messages = _serialize_messages(ctx.messages)
    if ctx.system:
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": ctx.system})
    options: dict[str, Any] = {"num_ctx": num_ctx}
    if ctx.temperature is not None:
        options["temperature"] = ctx.temperature
    if ctx.max_tokens is not None:
        options["num_predict"] = ctx.max_tokens
    if ctx.stop_sequences:
        options["stop"] = list(ctx.stop_sequences)
    payload: dict[str, Any] = {
        "model": ctx.model.id,
        "messages": messages,
        "stream": stream,
        "options": options,
    }
    if ctx.tools:
        payload["tools"] = _serialize_tools(ctx.tools)
    return payload


def _normalize_tool_call(
    tc: dict[str, Any], idx: int, trace_id: str
) -> tuple[str, str, str] | None:
    """Return (id, name, json_arguments) or None if not parseable."""
    fn = tc.get("function") or {}
    name = fn.get("name") or ""
    if not name:
        return None
    args = fn.get("arguments")
    if isinstance(args, dict):
        args_str = json.dumps(args, ensure_ascii=False)
    elif isinstance(args, str):
        args_str = args
    else:
        args_str = json.dumps(args or {}, ensure_ascii=False)
    tc_id = tc.get("id") or f"call_{trace_id}_{idx}"
    return tc_id, name, args_str


async def _request_ollama_nonstream(
    ctx: Context,
    opts: SimpleStreamOptions,
    *,
    payload_patch: PayloadPatch | None,
    base_url: str,
) -> AsyncIterator[AssistantMessageEvent]:
    num_ctx = await _resolve_num_ctx(base_url, ctx.model.id)
    payload = build_ollama_payload(ctx, stream=False, num_ctx=num_ctx)
    if opts.extra_params:
        payload.update(opts.extra_params)
    if payload_patch is not None:
        payload = payload_patch(payload)
    payload["stream"] = False

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if ctx.api.api_key:
        headers["Authorization"] = f"Bearer {ctx.api.api_key}"
    headers.update(ctx.api.extra_headers)

    url = base_url + "/api/chat"

    trace_id = llm_trace.new_request_id()
    trace_t0 = time.monotonic()
    llm_trace.log_request(
        request_id=trace_id,
        provider="ollama",
        model_id=ctx.model.id,
        url=url,
        payload=payload,
    )

    timeout = aiohttp.ClientTimeout(total=opts.timeout_seconds)
    data: dict[str, Any] | None = None
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    llm_trace.log_error(
                        request_id=trace_id,
                        provider="ollama",
                        model_id=ctx.model.id,
                        status=resp.status,
                        message=body,
                        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
                    )
                    yield ErrorEvent(
                        message=f"HTTP {resp.status}: {body[:300]}",
                        retryable=resp.status in RETRYABLE_STATUS,
                    )
                    return
                data = await resp.json()
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            llm_trace.log_error(
                request_id=trace_id,
                provider="ollama",
                model_id=ctx.model.id,
                status=None,
                message=f"connection error: {exc}",
                duration_ms=(time.monotonic() - trace_t0) * 1000.0,
            )
            yield ErrorEvent(message=f"connection error: {exc}", retryable=True, error=exc)
            return

    msg = (data or {}).get("message") or {}
    content_text = msg.get("content") or ""
    reasoning = msg.get("thinking")
    raw_tcs = msg.get("tool_calls") or []
    finish_reason = (data or {}).get("done_reason") or "stop"
    if raw_tcs:
        finish_reason = "tool_calls"
    usage = {
        "prompt_tokens": (data or {}).get("prompt_eval_count"),
        "completion_tokens": (data or {}).get("eval_count"),
        "total_tokens": ((data or {}).get("prompt_eval_count") or 0)
        + ((data or {}).get("eval_count") or 0),
    }

    if isinstance(reasoning, str) and reasoning:
        yield ThinkingDeltaEvent(delta=reasoning)
    if isinstance(content_text, str) and content_text:
        yield TextDeltaEvent(delta=content_text)

    tool_calls_out: list[dict[str, Any]] = []
    for i, tc in enumerate(raw_tcs):
        norm = _normalize_tool_call(tc, i, trace_id)
        if norm is None:
            continue
        tc_id, name, args_str = norm
        tool_calls_out.append({"id": tc_id, "name": name, "arguments": args_str})
        yield ToolUseStartEvent(id=tc_id, name=name)
        if args_str:
            yield ToolUseInputDeltaEvent(id=tc_id, input_delta=args_str)
        yield ToolUseEndEvent(id=tc_id)

    yield UsageEvent(usage=usage)
    yield StopEvent(reason=finish_reason)

    llm_trace.log_response(
        request_id=trace_id,
        provider="ollama",
        model_id=ctx.model.id,
        content=content_text,
        tool_calls=tool_calls_out,
        finish_reason=finish_reason,
        usage=usage,
        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
    )


async def stream_ollama_native(
    ctx: Context,
    opts: SimpleStreamOptions,
    *,
    payload_patch: PayloadPatch | None = None,
) -> AsyncIterator[AssistantMessageEvent]:
    """Native Ollama `/api/chat` event stream → AssistantMessageEvent.

    For tool-using rounds we run non-streaming: native streaming only
    surfaces `tool_calls` in the final `done` frame anyway, and
    nonstream gives us a single deterministic JSON to parse.
    """
    base_url = _native_base_url(ctx.api.base_url)

    if ctx.tools:
        async for ev in _request_ollama_nonstream(
            ctx, opts, payload_patch=payload_patch, base_url=base_url
        ):
            yield ev
        return

    num_ctx = await _resolve_num_ctx(base_url, ctx.model.id)
    payload = build_ollama_payload(ctx, stream=True, num_ctx=num_ctx)
    if opts.extra_params:
        payload.update(opts.extra_params)
    if payload_patch is not None:
        payload = payload_patch(payload)
    payload["stream"] = True

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/x-ndjson",
    }
    if ctx.api.api_key:
        headers["Authorization"] = f"Bearer {ctx.api.api_key}"
    headers.update(ctx.api.extra_headers)

    url = base_url + "/api/chat"

    trace_id = llm_trace.new_request_id()
    trace_t0 = time.monotonic()
    llm_trace.log_request(
        request_id=trace_id,
        provider="ollama",
        model_id=ctx.model.id,
        url=url,
        payload=payload,
    )

    text_parts: list[str] = []
    finish_reason: str | None = None
    usage_out: dict[str, Any] | None = None
    tool_calls_out: list[dict[str, Any]] = []

    timeout = aiohttp.ClientTimeout(total=opts.timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    llm_trace.log_error(
                        request_id=trace_id,
                        provider="ollama",
                        model_id=ctx.model.id,
                        status=resp.status,
                        message=body,
                        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
                    )
                    yield ErrorEvent(
                        message=f"HTTP {resp.status}: {body[:300]}",
                        retryable=resp.status in RETRYABLE_STATUS,
                    )
                    return

                async for raw in resp.content:
                    if opts.abort_event is not None and opts.abort_event.is_set():
                        return
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = event.get("message") or {}

                    thinking = msg.get("thinking")
                    if isinstance(thinking, str) and thinking:
                        yield ThinkingDeltaEvent(delta=thinking)

                    delta = msg.get("content")
                    if isinstance(delta, str) and delta:
                        text_parts.append(delta)
                        yield TextDeltaEvent(delta=delta)

                    if event.get("done"):
                        finish_reason = event.get("done_reason") or "stop"
                        usage_out = {
                            "prompt_tokens": event.get("prompt_eval_count"),
                            "completion_tokens": event.get("eval_count"),
                            "total_tokens": (event.get("prompt_eval_count") or 0)
                            + (event.get("eval_count") or 0),
                        }
                        # Native may dump batched tool_calls in the done frame
                        # even on a streaming run.
                        for i, tc in enumerate(msg.get("tool_calls") or []):
                            norm = _normalize_tool_call(tc, i, trace_id)
                            if norm is None:
                                continue
                            tc_id, name, args_str = norm
                            tool_calls_out.append(
                                {"id": tc_id, "name": name, "arguments": args_str}
                            )
                            yield ToolUseStartEvent(id=tc_id, name=name)
                            if args_str:
                                yield ToolUseInputDeltaEvent(id=tc_id, input_delta=args_str)
                            yield ToolUseEndEvent(id=tc_id)
                            finish_reason = "tool_calls"
                        if usage_out:
                            yield UsageEvent(usage=usage_out)
                        yield StopEvent(reason=finish_reason)
                        break
        except (TimeoutError, aiohttp.ClientConnectionError) as exc:
            llm_trace.log_error(
                request_id=trace_id,
                provider="ollama",
                model_id=ctx.model.id,
                status=None,
                message=f"connection error: {exc}",
                duration_ms=(time.monotonic() - trace_t0) * 1000.0,
            )
            yield ErrorEvent(message=f"connection error: {exc}", retryable=True, error=exc)
            return

    llm_trace.log_response(
        request_id=trace_id,
        provider="ollama",
        model_id=ctx.model.id,
        content="".join(text_parts),
        tool_calls=tool_calls_out,
        finish_reason=finish_reason,
        usage=usage_out,
        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
    )


async def _ollama_native_fn(
    ctx: Context, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    async for ev in stream_ollama_native(ctx, opts):
        yield ev


register_provider_stream("ollama", _ollama_native_fn)


__all__ = [
    "build_ollama_payload",
    "stream_ollama_native",
]
