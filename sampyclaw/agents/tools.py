"""Tool registry and Anthropic-format tool schema exporter.

Port of the tool-registry portion of openclaw `src/agents/*`. A tool is any
callable with (name, description, JSON input schema, async execute). Tools
render to the exact shape Anthropic's Messages API expects via
`ToolRegistry.as_anthropic_tools()`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class Tool(Protocol):
    """Minimum tool contract. Implementations may add fields for internal use."""

    name: str
    description: str
    input_schema: dict[str, Any]

    async def execute(self, args: dict[str, Any]) -> str: ...


M = TypeVar("M", bound=BaseModel)


class FunctionTool(Generic[M]):
    """Wrap a Pydantic-typed handler as a Tool.

    `handler` receives a parsed Pydantic instance and returns `str` (or an
    awaitable of `str`). Errors during schema validation or execution surface
    as the raised exception; callers (the agent) decide how to format them.
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[M],
        handler: Callable[[M], str | Awaitable[str]],
    ) -> None:
        if not name:
            raise ValueError("tool name is required")
        self.name = name
        self.description = description
        self._input_model = input_model
        self._handler = handler

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_model.model_json_schema()

    async def execute(self, args: dict[str, Any]) -> str:
        parsed = self._input_model.model_validate(args)
        result = self._handler(parsed)
        if isinstance(result, str):
            return result
        return await result  # type: ignore[no-any-return]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def as_anthropic_tools(self) -> list[dict[str, Any]]:
        """Render the registry as Anthropic Messages API `tools` param."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def as_openai_tools(self) -> list[dict[str, Any]]:
        """Render the registry as OpenAI chat.completions `tools` param.

        Works for any OpenAI-compatible server (Ollama, LM Studio, vLLM,
        llama.cpp, TGI) that supports function calling.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in self._tools.values()
        ]
