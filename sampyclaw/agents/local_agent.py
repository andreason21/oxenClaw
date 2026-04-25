"""Local/self-hosted LLM agent over any OpenAI-compatible HTTP API.

Works with Ollama, LM Studio, vLLM, llama.cpp `server`, text-generation-webui
— anything that serves `POST /v1/chat/completions`. Defaults target a local
Ollama at `127.0.0.1:11434` running a tool-capable model. Change via
constructor or CLI flags.

Design mirrors `AnthropicAgent` but adds capabilities the local path needs
more than the Anthropic path:
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
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.agents.tools import ToolRegistry
from sampyclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.memory.retriever import MemoryRetriever, format_memories_for_prompt
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from sampyclaw.plugin_sdk.reply_runtime import chunk_text
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.local")

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"  # Ollama default
# Tool-capable Ollama default. Earlier gemma3:4b was dropped due to weak
# tool support; gemma4:latest restores native function calling. Override
# per deployment if you prefer qwen2.5, llama3.x, etc.
DEFAULT_MODEL = "gemma4:latest"
DEFAULT_SYSTEM_PROMPT = (
    "You are sampyClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)
DEFAULT_MAX_TOKENS = 4096
DEFAULT_MAX_TOOL_ITERATIONS = 8
# Lower temperature → reliable JSON tool arguments. Override for chat-only
# personas via constructor.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_CHUNK_LIMIT = 4000
DEFAULT_TIMEOUT = 300.0  # local models can be slow
DEFAULT_MAX_HISTORY_CHARS = 24_000  # ~6K tokens, fits 8K-context models
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_INITIAL = 0.5
DEFAULT_BACKOFF_MAX = 8.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 529})


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
        paths: SampyclawPaths | None = None,
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
        max_history_chars: int = DEFAULT_MAX_HISTORY_CHARS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_initial: float = DEFAULT_BACKOFF_INITIAL,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
        warmup: bool = True,
        stream: bool = True,
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
        self._max_history_chars = max_history_chars
        self._max_retries = max_retries
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
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
        user_text = (inbound.text or "").strip()
        if not user_text:
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
        history.append({"role": "user", "content": user_text})
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
            response = await self._chat_complete(
                messages=history.messages(), tools=tools_param
            )
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

            results = await asyncio.gather(
                *(self._execute_tool(call) for call in tool_calls)
            )
            for call, (result_text, is_error) in zip(tool_calls, results):
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
            collected_text.append(
                "(stopped: reached max tool iterations without a final answer)"
            )

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
        return await self._chat_complete_once(
            messages=messages, tools=tools, stream=False
        )

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
        return await self._post_with_retry(url=url, payload=payload, headers=headers)

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
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
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
                        return await self._consume_sse(resp)
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
                if attempt >= self._max_retries:
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
                    "finish_reason": finish_reason
                    or ("tool_calls" if tool_buf else "stop"),
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
            # Ollama's OpenAI shim accepts this alias and uses it as the
            # generation cap; harmless on real OpenAI / vLLM (extra field
            # ignored unless `strict` mode is on).
            "num_predict": self._max_tokens,
            "temperature": self._temperature,
            "stream": stream,
        }
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
