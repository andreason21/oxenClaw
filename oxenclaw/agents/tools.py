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


def _canonicalise_tool_name(name: str) -> str:
    """Normalise an LLM-emitted tool name so common drift collapses to
    one canonical key. Handles `:` / `.` / `-` separators (openclaw-
    namespaced `memory:set_fact`, RPC-style `memory.save`, hyphen
    forms) and case variations. Pure name-shape canonicalisation —
    semantic aliasing (e.g. `set_fact` → `save`) lives in
    `_TOOL_NAME_ALIASES`."""
    return name.strip().lower().replace(":", "_").replace(".", "_").replace("-", "_")


# Registry-level aliases: every key here is the canonicalised form an
# LLM might emit instead of the canonical tool name. Mirrors openclaw's
# permissive tool resolver. Add lines here when production logs surface
# new hallucinated names — `tool 'X' is not registered` is the signal.
_TOOL_NAME_ALIASES: dict[str, str] = {
    # memory_save variants
    "memory_set_fact": "memory_save",      # openclaw-style colon namespace
    "set_fact": "memory_save",
    "remember": "memory_save",
    "remember_fact": "memory_save",
    "save_memory": "memory_save",
    "memory_set": "memory_save",
    "memory_add": "memory_save",
    "memory_save_fact": "memory_save",
    "memory_save_to_inbox": "memory_save",
    "store_memory": "memory_save",
    "memory_remember": "memory_save",
    # memory_search variants
    "memory_recall": "memory_search",
    "recall": "memory_search",
    "memory_query": "memory_search",
    "memory_lookup": "memory_search",
    "memory_find": "memory_search",
    "memory_get_fact": "memory_search",
    # memory_get variants
    "memory_read": "memory_get",
    "memory_fetch": "memory_get",
    # wiki tool variants
    "wiki_create": "wiki_save",
    "wiki_update": "wiki_save",
    "wiki_edit": "wiki_save",
    "wiki_query": "wiki_search",
    "wiki_lookup": "wiki_search",
    # skill resolver variants
    "skill_install": "skill_resolver",
    "skill_find": "skill_resolver",
    "find_skill": "skill_resolver",
    "install_skill": "skill_resolver",
}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # Keyed by canonicalised name for fast alias-tolerant lookup.
        self._by_canon: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        self._by_canon[_canonicalise_tool_name(tool.name)] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    def get(self, name: str) -> Tool | None:
        # 1) Exact name (fast path).
        direct = self._tools.get(name)
        if direct is not None:
            return direct
        # 2) Canonicalised lookup — handles `memory:set_fact` /
        #    `memory.save` / `Memory_Save` style drift.
        canon = _canonicalise_tool_name(name)
        canon_hit = self._by_canon.get(canon)
        if canon_hit is not None:
            return canon_hit
        # 3) Semantic alias table — handles outright wrong-but-plausible
        #    names like `remember`, `set_fact`, `save_memory`.
        aliased = _TOOL_NAME_ALIASES.get(canon)
        if aliased is not None:
            return self._tools.get(aliased)
        return None

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
