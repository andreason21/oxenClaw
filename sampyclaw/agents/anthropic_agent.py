"""Anthropic-backed agent with tool use + prompt caching.

Implements the `Agent` Protocol. The inference loop:
  1. append user turn to history
  2. call `messages.create` with system, tools, history
  3. capture assistant content (text + tool_use)
  4. if `stop_reason == "tool_use"`, execute each tool, append results, loop
  5. otherwise emit accumulated text as SendParams (chunked for channel limits)

Caching: system prompt carries `cache_control: {"type": "ephemeral"}`.
Streaming is deferred — non-streaming is enough for gateway-mediated delivery
since we don't use edit-streaming yet (that's a future refinement).

Port of openclaw `src/agents/inference-loop.ts` narrowed to the Anthropic
provider. Model defaults to `claude-sonnet-4-6` (current Sonnet).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sampyclaw.agents.base import Agent, AgentContext
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.agents.tools import ToolRegistry
from sampyclaw.clawhub.loader import format_skills_for_prompt, load_installed_skills
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams
from sampyclaw.plugin_sdk.reply_runtime import chunk_text
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.anthropic")

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_SYSTEM_PROMPT = (
    "You are sampyClaw, a helpful assistant reached via chat channels. "
    "Be concise. Use tools when helpful."
)
DEFAULT_MAX_TOOL_ITERATIONS = 8
TELEGRAM_TEXT_LIMIT = 4000  # leave headroom below Telegram's 4096


class AnthropicAgent:
    """LLM-backed agent. Implements Agent Protocol from sampyclaw.agents.base."""

    def __init__(
        self,
        *,
        agent_id: str = "anthropic",
        client: Any | None = None,
        tools: ToolRegistry | None = None,
        paths: SampyclawPaths | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        chunk_limit: int = TELEGRAM_TEXT_LIMIT,
        include_skills: bool = True,
    ) -> None:
        if max_tool_iterations < 1:
            raise ValueError("max_tool_iterations must be >= 1")
        if chunk_limit < 1:
            raise ValueError("chunk_limit must be positive")
        self.id = agent_id
        self._client = client
        self._tools = tools or ToolRegistry()
        self._paths = paths or default_paths()
        self._system_prompt = system_prompt
        self._model = model
        self._max_tokens = max_tokens
        self._max_tool_iterations = max_tool_iterations
        self._chunk_limit = chunk_limit
        self._include_skills = include_skills

    def _build_system_prompt(self) -> str:
        if not self._include_skills:
            return self._system_prompt
        skills = load_installed_skills(self._paths)
        block = format_skills_for_prompt(skills)
        if not block:
            return self._system_prompt
        return f"{self._system_prompt}\n\n{block}"

    def _ensure_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    def _history_for(self, session_key: str) -> ConversationHistory:
        return ConversationHistory(self._paths.session_file(self.id, session_key))

    async def handle(
        self, inbound: InboundEnvelope, ctx: AgentContext
    ) -> AsyncIterator[SendParams]:
        user_text = (inbound.text or "").strip()
        if not user_text:
            return

        history = self._history_for(ctx.session_key)
        history.append({"role": "user", "content": user_text})

        reply_text = await self._run_inference_loop(history)
        history.save()

        if not reply_text:
            return
        for chunk in chunk_text(reply_text, self._chunk_limit):
            yield SendParams(target=inbound.target, text=chunk)

    async def _run_inference_loop(self, history: ConversationHistory) -> str:
        client = self._ensure_client()
        tools_param = self._tools.as_anthropic_tools()
        system_param = [
            {
                "type": "text",
                "text": self._build_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        collected_text: list[str] = []

        for iteration in range(self._max_tool_iterations):
            kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": self._max_tokens,
                "system": system_param,
                "messages": history.messages(),
            }
            if tools_param:
                kwargs["tools"] = tools_param

            response = await client.messages.create(**kwargs)

            assistant_blocks, text_parts, tool_calls = _partition_blocks(response.content)
            history.append({"role": "assistant", "content": assistant_blocks})
            if text_parts:
                collected_text.extend(text_parts)

            if response.stop_reason != "tool_use" or not tool_calls:
                break

            tool_results = await self._execute_tools(tool_calls)
            history.append({"role": "user", "content": tool_results})
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

    async def _execute_tools(self, tool_calls: list[Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for call in tool_calls:
            tool = self._tools.get(call.name)
            if tool is None:
                output = f"tool {call.name!r} is not registered"
                is_error = True
            else:
                try:
                    output = await tool.execute(dict(call.input))
                    is_error = False
                except Exception as exc:
                    logger.exception("tool %s raised", call.name)
                    output = f"tool error: {exc}"
                    is_error = True
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": output,
            }
            if is_error:
                block["is_error"] = True
            results.append(block)
        return results


def _partition_blocks(content: list[Any]) -> tuple[list[dict[str, Any]], list[str], list[Any]]:
    """Split an Anthropic response into (serialised blocks, text parts, tool_use calls)."""
    serialised: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_calls: list[Any] = []
    for block in content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            serialised.append({"type": "text", "text": text})
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            serialised.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
            tool_calls.append(block)
    return serialised, text_parts, tool_calls
