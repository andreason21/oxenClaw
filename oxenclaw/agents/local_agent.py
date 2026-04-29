"""Local/self-hosted LLM agent over any OpenAI-compatible HTTP API.

Works with Ollama, LM Studio, vLLM, llama.cpp `server`, text-generation-webui
— anything that serves `POST /v1/chat/completions`. Defaults target a local
Ollama at `127.0.0.1:11434` running a tool-capable model. Change via
constructor or CLI flags.

Design adds capabilities the local path needs more than a hosted-API
path:
- Streaming (`stream=True`) with delta accumulation for content + tool_calls.
- Retry/backoff on transient errors (429, 5xx, connection drops, timeouts).
- One-shot warmup ping so the first user request doesn't pay model load.
- Token-usage logging per turn; `num_predict` shadow of `max_tokens` for
  the Ollama-specific OpenAI shim.
- Sliding-window context truncation before each request.
- Parallel tool execution when the assistant returns multiple `tool_use`s.
- JSON-parse self-correct: bad tool arguments are fed back as a tool error
  so the model can retry within `max_tool_iterations`.

Tool-call message format follows OpenAI spec (`tool_calls` list +
`{"role": "tool", "tool_call_id": ...}` results).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

import aiohttp

from oxenclaw.agents.base import AgentContext
from oxenclaw.observability import llm_trace
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from oxenclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from oxenclaw.plugin_sdk.reply_runtime import chunk_text
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.local")

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"  # Ollama default
# Tool-capable Ollama default. gemma3:4b was dropped due to weak tool
# support; gemma4:latest then served as default. Switched to qwen3.5:9b
# on 2026-04-28 after a live PiAgent e2e gate (18/18 PASS, see
# /tmp/qwen_live_e2e.py): same vision + native tools, 256K ctx (vs 128K
# on gemma4:latest), ~6.6 GB Q4_K_M (vs ~9.6 GB), and stronger tool-arg
# fidelity on Korean prompts. Override per deployment as needed.
DEFAULT_MODEL = "qwen3.5:9b"
# vLLM canonical default (`vllm serve --port 8000` lands here). Override
# with `--base-url http://internal-vllm:8000/v1` for a remote server.
VLLM_DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"

# Provider "flavor" tells the agent which OpenAI-shape quirks to honour.
# - "ollama"  → send `num_predict` alongside `max_tokens`; one-shot warmup
#   ping default-on (model load latency).
# - "vllm"    → strict-OpenAI; skip `num_predict` (vLLM ignores most
#   extras but some deployments enable strict schemas); skip warmup
#   (vLLM has the weights resident).
# - "openai"  → strict-OpenAI; same as vllm but kept distinct so future
#   provider-specific knobs (eg. cache headers) don't bleed.
FlavorLiteral = Literal["ollama", "vllm", "openai"]
DEFAULT_SYSTEM_PROMPT = (
    "You are oxenClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_TOOL_ITERATIONS = 8
# Lower temperature → reliable JSON tool arguments. Override for chat-only
# personas via constructor.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_CHUNK_LIMIT = 4000
DEFAULT_TIMEOUT = 300.0  # local models can be slow
# Conservative *floor* — kept low to fit small-context models like
# `gemma3:4b` (8K). When the runtime knows the actual model's context
# window (via `pi.tokens.model_context_window`), it scales this up to
# roughly half the window so large-context models like `qwen3.5:9b`
# (256K) or `gemma4:latest` (128K) actually use the room they have.
# The constructor still accepts an explicit override.
DEFAULT_MAX_HISTORY_CHARS = 24_000  # ~6K tokens, safe even for 8K-ctx
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_INITIAL = 0.5
DEFAULT_BACKOFF_MAX = 8.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})


def _derive_history_budget_chars(model_id: str) -> int:
    """Pick a reasonable `max_history_chars` based on the model's window.

    Heuristic: leave half the context window for system prompt + tools +
    the new turn + the model's reply. Use ~3.5 chars/token (English-ish
    ratio also used by `pi.tokens.estimate_tokens`).

    Floor at `DEFAULT_MAX_HISTORY_CHARS` so we never under-cut a model
    that reports an unusually small (or unknown) context window.
    """
    from oxenclaw.pi.tokens import model_context_window

    window_tokens = model_context_window(model_id, default=8_192)
    budget_tokens = max(2_048, window_tokens // 2)
    chars = int(budget_tokens * 3.5)
    return max(DEFAULT_MAX_HISTORY_CHARS, chars)


class LocalAgent:
    """Implements `Agent` Protocol backed by an OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        *,
        agent_id: str = "local",
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        tools: ToolRegistry | None = None,
        paths: OxenclawPaths | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        temperature: float = DEFAULT_TEMPERATURE,
        chunk_limit: int = DEFAULT_CHUNK_LIMIT,
        timeout: float = DEFAULT_TIMEOUT,
        http_session: aiohttp.ClientSession | None = None,
        include_skills: bool = True,
        memory: MemoryRetriever | None = None,
        memory_top_k: int = 5,
        max_history_chars: int | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        warmup: bool | None = None,
        stream: bool = True,
        flavor: FlavorLiteral = "ollama",
    ) -> None:
        if max_tool_iterations < 1:
            raise ValueError("max_tool_iterations must be >= 1")
        if chunk_limit < 1:
            raise ValueError("chunk_limit must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.id = agent_id
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._tools = tools or ToolRegistry()
        self._paths = paths or default_paths()
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._max_tool_iterations = max_tool_iterations
        self._temperature = temperature
        self._chunk_limit = chunk_limit
        self._timeout = timeout
        self._http: aiohttp.ClientSession | None = http_session
        self._owns_session = http_session is None
        self._include_skills = include_skills
        self._memory = memory
        self._memory_top_k = memory_top_k
        self._max_history_chars = (
            max_history_chars
            if max_history_chars is not None
            else _derive_history_budget_chars(model)
        )
        self._max_retries = max_retries
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._flavor: FlavorLiteral = flavor
        # Warmup default depends on flavor: Ollama needs it (cold load),
        # vLLM/openai already have weights resident.
        if warmup is None:
            warmup = flavor == "ollama"
        self._warmup_pending = warmup
        self._warmup_lock = asyncio.Lock()
        self._stream = stream

    def _build_system_prompt(self, memory_block: str = "") -> str:
        parts = [self._system_prompt]
        if self._include_skills:
            block = format_skills_for_prompt(load_installed_skills(self._paths))
            if block:
                parts.append(block)
        if memory_block:
            parts.append(memory_block)
        return "\n\n".join(parts)

    async def _retrieve_memories(self, ctx: AgentContext, query: str) -> str:
        if self._memory is None or not query.strip():
            return ""
        try:
            hits = await self._memory.search(query=query, k=self._memory_top_k)
        except Exception:
            logger.exception("memory recall failed; continuing without it")
            return ""
        return format_memories_for_prompt(hits)

    async def handle(
        self, inbound: InboundEnvelope, ctx: AgentContext
    ) -> AsyncIterator[SendParams]:
        from oxenclaw.multimodal import (
            model_supports_images,
            normalize_inbound_images,
            openai_image_url_block,
        )

        user_text = (inbound.text or "").strip()

        # Resolve image attachments. Only the OpenAI-shape `image_url`
        # block is sent to Ollama (which speaks an OpenAI-compatible
        # API). Models that don't support images get a textual note so
        # they know context was lost.
        images: list = []
        dropped_notes: list[str] = []
        if inbound.media:
            if model_supports_images(self._model):
                images, dropped_notes = await normalize_inbound_images(inbound.media)
            else:
                photo_count = sum(1 for m in inbound.media if m.kind == "photo")
                if photo_count:
                    dropped_notes.append(
                        f"({photo_count} image(s) dropped: model "
                        f"{self._model!r} does not support image input)"
                    )

        if not user_text and not images and not dropped_notes:
            return

        await self._maybe_warmup()

        history = self._history_for(ctx.session_key)
        memory_block = await self._retrieve_memories(ctx, user_text)
        if len(history) == 0:
            history.append(
                {
                    "role": "system",
                    "content": self._build_system_prompt(memory_block=memory_block),
                }
            )
        elif memory_block and history.messages()[0].get("role") == "system":
            base_text = self._build_system_prompt(memory_block=memory_block)
            history._messages[0] = {
                "role": "system",
                "content": base_text,
            }

        # Build user message: list-of-blocks when there are images, plain
        # string when text-only (smaller payload, no semantic difference
        # — Ollama's OpenAI shim accepts both).
        if images:
            content_blocks: list[dict] = [openai_image_url_block(img) for img in images]
            text_parts = [t for t in (user_text, *dropped_notes) if t]
            if text_parts:
                content_blocks.append({"type": "text", "text": "\n".join(text_parts)})
            history.append({"role": "user", "content": content_blocks})
        else:
            combined = "\n".join(t for t in (user_text, *dropped_notes) if t)
            history.append({"role": "user", "content": combined})
        dropped = history.truncate_to_window(max_chars=self._max_history_chars)
        if dropped:
            logger.info(
                "agent %s truncated %d old messages (window=%d chars)",
                self.id,
                dropped,
                self._max_history_chars,
            )

        reply_text = await self._run_inference_loop(history)
        history.save()

        if not reply_text:
            return
        for chunk in chunk_text(reply_text, self._chunk_limit):
            yield SendParams(target=inbound.target, text=chunk)

    async def _run_inference_loop(self, history: ConversationHistory) -> str:
        tools_param = self._tools.as_openai_tools()
        collected_text: list[str] = []

        for _ in range(self._max_tool_iterations):
            response = await self._chat_complete(messages=history.messages(), tools=tools_param)
            self._log_usage(response)
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason")
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            history.append(assistant_msg)
            if content:
                collected_text.append(content)

            if finish_reason != "tool_calls" or not tool_calls:
                break

            results = await asyncio.gather(*(self._execute_tool(call) for call in tool_calls))
            for call, (result_text, is_error) in zip(tool_calls, results, strict=False):
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": result_text,
                        **({"is_error": True} if is_error else {}),
                    }
                )
        else:
            logger.warning(
                "agent %s hit max_tool_iterations=%d",
                self.id,
                self._max_tool_iterations,
            )
            collected_text.append("(stopped: reached max tool iterations without a final answer)")

        return "\n".join(p for p in collected_text if p).strip()

    async def _execute_tool(self, call: dict[str, Any]) -> tuple[str, bool]:
        function = call.get("function") or {}
        name = function.get("name", "")
        raw_args = function.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError as exc:
            # Self-correct path: feed the parse error back so the model can
            # retry with valid JSON in the next iteration.
            return (
                f"tool error: arguments were not valid JSON ({exc}). "
                f"Re-emit the call with a JSON object literal.",
                True,
            )
        if not isinstance(args, dict):
            return (
                f"tool error: arguments must be a JSON object, got {type(args).__name__}. "
                f"Re-emit the call with an object.",
                True,
            )

        tool = self._tools.get(name)
        if tool is None:
            return f"tool {name!r} is not registered", True
        try:
            return await tool.execute(args), False
        except Exception as exc:
            logger.exception("tool %s raised", name)
            return f"tool error: {exc}", True

    async def _chat_complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if self._stream:
            return await self._chat_complete_stream(messages=messages, tools=tools)
        return await self._chat_complete_once(messages=messages, tools=tools, stream=False)

    async def _chat_complete_once(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
    ) -> dict[str, Any]:
        payload = self._build_payload(messages=messages, tools=tools, stream=stream)
        headers = self._build_headers()
        url = f"{self._base_url}/chat/completions"
        trace_id = llm_trace.new_request_id()
        trace_t0 = time.monotonic()
        llm_trace.log_request(
            request_id=trace_id,
            provider=self._flavor,
            model_id=self._model,
            url=url,
            payload=payload,
        )
        try:
            response = await self._post_with_retry(url=url, payload=payload, headers=headers)
        except Exception as exc:
            llm_trace.log_error(
                request_id=trace_id,
                provider=self._flavor,
                model_id=self._model,
                status=getattr(exc, "status", None),
                message=str(exc),
                duration_ms=(time.monotonic() - trace_t0) * 1000.0,
            )
            raise
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        llm_trace.log_response(
            request_id=trace_id,
            provider=self._flavor,
            model_id=self._model,
            content=message.get("content") or "",
            tool_calls=list(message.get("tool_calls") or []),
            finish_reason=choice.get("finish_reason"),
            usage=response.get("usage") if isinstance(response.get("usage"), dict) else None,
            duration_ms=(time.monotonic() - trace_t0) * 1000.0,
        )
        return response

    async def _post_with_retry(
        self,
        *,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        attempt = 0
        last_exc: Exception | None = None
        while True:
            session = await self._ensure_session()
            try:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status in RETRYABLE_STATUS and attempt < self._max_retries:
                        body = await resp.text()
                        logger.warning(
                            "agent %s retryable HTTP %d (attempt %d/%d): %s",
                            self.id,
                            resp.status,
                            attempt + 1,
                            self._max_retries,
                            body[:200],
                        )
                    else:
                        resp.raise_for_status()
                        return await resp.json()
            except (TimeoutError, aiohttp.ClientConnectionError) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise
                logger.warning(
                    "agent %s transient error (attempt %d/%d): %s",
                    self.id,
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
            attempt += 1
            await asyncio.sleep(self._backoff_delay(attempt))
        # Unreachable; satisfy type-checkers.
        if last_exc:  # pragma: no cover
            raise last_exc
        raise RuntimeError("retry loop exited unexpectedly")  # pragma: no cover

    def _backoff_delay(self, attempt: int) -> float:
        base = min(self._backoff_max, self._backoff_initial * (2 ** (attempt - 1)))
        # Full jitter (AWS-style): uniform [0, base].
        return random.uniform(0, base)

    async def _chat_complete_stream(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Stream SSE chunks and assemble a non-streaming response shape.

        Falls back to non-streaming on the first attempt's error so callers
        that don't care about TTFT still get a response.
        """
        payload = self._build_payload(messages=messages, tools=tools, stream=True)
        headers = self._build_headers()
        url = f"{self._base_url}/chat/completions"

        trace_id = llm_trace.new_request_id()
        trace_t0 = time.monotonic()
        llm_trace.log_request(
            request_id=trace_id,
            provider=self._flavor,
            model_id=self._model,
            url=url,
            payload=payload,
        )

        attempt = 0
        while True:
            session = await self._ensure_session()
            try:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    if resp.status in RETRYABLE_STATUS and attempt < self._max_retries:
                        body = await resp.text()
                        logger.warning(
                            "agent %s stream retryable HTTP %d (attempt %d/%d): %s",
                            self.id,
                            resp.status,
                            attempt + 1,
                            self._max_retries,
                            body[:200],
                        )
                    else:
                        resp.raise_for_status()
                        envelope = await self._consume_sse(resp)
                        choice = (envelope.get("choices") or [{}])[0]
                        message = choice.get("message") or {}
                        llm_trace.log_response(
                            request_id=trace_id,
                            provider=self._flavor,
                            model_id=self._model,
                            content=message.get("content") or "",
                            tool_calls=list(message.get("tool_calls") or []),
                            finish_reason=choice.get("finish_reason"),
                            usage=envelope.get("usage")
                            if isinstance(envelope.get("usage"), dict)
                            else None,
                            duration_ms=(time.monotonic() - trace_t0) * 1000.0,
                        )
                        return envelope
            except (TimeoutError, aiohttp.ClientConnectionError) as exc:
                if attempt >= self._max_retries:
                    llm_trace.log_error(
                        request_id=trace_id,
                        provider=self._flavor,
                        model_id=self._model,
                        status=None,
                        message=f"connection error: {exc}",
                        duration_ms=(time.monotonic() - trace_t0) * 1000.0,
                    )
                    raise
                logger.warning(
                    "agent %s stream transient error (attempt %d/%d): %s",
                    self.id,
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
            attempt += 1
            await asyncio.sleep(self._backoff_delay(attempt))

    async def _consume_sse(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        """Collapse OpenAI-style SSE deltas into a single response envelope."""
        content_parts: list[str] = []
        # tool_calls are sent as deltas keyed by `index`; assemble per index.
        tool_buf: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None

        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                content_parts.append(piece)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_buf.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc.get("id"):
                    slot["id"] = tc["id"]
                if tc.get("type"):
                    slot["type"] = tc["type"]
                fn_delta = tc.get("function") or {}
                if fn_delta.get("name"):
                    slot["function"]["name"] += fn_delta["name"]
                if fn_delta.get("arguments"):
                    slot["function"]["arguments"] += fn_delta["arguments"]

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_buf:
            message["tool_calls"] = [tool_buf[i] for i in sorted(tool_buf)]
        envelope: dict[str, Any] = {
            "choices": [
                {
                    "message": message,
                    "finish_reason": finish_reason or ("tool_calls" if tool_buf else "stop"),
                }
            ]
        }
        if usage is not None:
            envelope["usage"] = usage
        return envelope

    def _build_payload(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": stream,
        }
        if self._flavor == "ollama":
            # Ollama's OpenAI shim accepts this alias and uses it as the
            # generation cap. vLLM / OpenAI strict schemas reject it, so
            # we only send it on Ollama.
            payload["num_predict"] = self._max_tokens
        if stream:
            # Ask for usage in the final SSE chunk where supported.
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
        return payload

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _log_usage(self, response: dict[str, Any]) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            return
        logger.info(
            "agent %s usage prompt=%s completion=%s total=%s",
            self.id,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )

    async def _maybe_warmup(self) -> None:
        if not self._warmup_pending:
            return
        async with self._warmup_lock:
            if not self._warmup_pending:
                return
            self._warmup_pending = False
            try:
                await self._chat_complete_once(
                    messages=[{"role": "user", "content": "ping"}],
                    tools=[],
                    stream=False,
                )
                logger.info("agent %s warmup ok (model=%s)", self.id, self._model)
            except Exception as exc:
                logger.warning(
                    "agent %s warmup failed (model=%s): %s — continuing",
                    self.id,
                    self._model,
                    exc,
                )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_session = True
        return self._http

    def _history_for(self, session_key: str) -> ConversationHistory:
        return ConversationHistory(self._paths.session_file(self.id, session_key))

    async def aclose(self) -> None:
        if self._owns_session and self._http is not None:
            await self._http.close()
            self._http = None
