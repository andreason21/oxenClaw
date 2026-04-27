"""Pluggable memory provider ABC + builtin wrapper + registry.

Mirrors hermes-agent's ``agent/memory_provider.py`` concept. The
abstract ``MemoryProvider`` exposes a stable lifecycle so external
backends (Honcho, Hindsight, Mem0, etc.) can plug in without touching
the agent core. The ``BuiltinMemoryProvider`` wraps the existing
``MemoryRetriever`` (inbox + vector + FTS) so the new ABC is the
default path; legacy callers pass a raw ``MemoryRetriever`` and
``PiAgent`` wraps it transparently.

Lifecycle (caller-driven, all optional except ``initialize`` and
``get_tool_schemas``):

  initialize(session_key)              — connect, warm up
  system_prompt_block(session_key)     — STATIC string for the system
                                          prompt; must be byte-stable
                                          across mid-session writes so
                                          the provider's prompt cache
                                          survives.
  prefetch(session_key, query)         — mid-turn dynamic recall
  sync_turn(session_key, user, asst)   — write after each turn
  get_tool_schemas()                   — tools to expose to the model
  handle_tool_call(name, args)         — dispatch a tool call
  shutdown()                           — clean exit

Optional hooks (override to opt in):
  on_pre_compress(messages) -> dict    — extract insights into the
                                          compactor prompt prefix
  on_session_end(session_key)          — final flush
  on_memory_write(text, tags)          — mirror built-in writes
  on_delegation(...)                   — observe subagent results
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from oxenclaw.agents.tools import Tool
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.provider")


# ─── Abstract base class ──────────────────────────────────────────────


class MemoryProvider(ABC):
    """Abstract base for pluggable memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'builtin', 'honcho', 'mem0')."""

    @abstractmethod
    async def initialize(self, session_key: str) -> None:
        """Initialize for a session. Idempotent — safe to call multiple times."""

    @abstractmethod
    def get_tool_schemas(self) -> list[Tool]:
        """Return tools to expose to the model. Empty list = context-only."""

    # The methods below have non-abstract default implementations so
    # providers only override what they need.

    async def system_prompt_block(self, session_key: str) -> str:
        """Static string for the system prompt.

        MUST be byte-stable across mid-session writes so the prompt
        cache survives. Dynamic per-query content belongs in
        ``prefetch`` (which is below the cache marker).
        """
        return ""

    async def prefetch(self, session_key: str, query: str) -> str:
        """Mid-turn dynamic recall returned as a rendered string."""
        return ""

    async def sync_turn(
        self,
        session_key: str,
        user_msg: str,
        assistant_msg: str,
    ) -> None:
        """Persist a completed turn. Default: no-op."""

    async def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call. Default raises if called for a name
        the provider doesn't actually own."""
        raise NotImplementedError(
            f"provider {self.name!r} does not handle tool {name!r}"
        )

    async def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # ── Optional hooks ────────────────────────────────────────────────

    async def on_pre_compress(self, messages: list[Any]) -> dict[str, Any]:
        """Called before context compression discards old messages.

        Return a dict of the form ``{"insights": [...]}`` to fold into
        the compactor prompt. Empty dict (default) skips folding.
        """
        return {}

    async def on_session_end(self, session_key: str) -> None:
        """Final flush at the end of a session."""

    async def on_memory_write(self, text: str, tags: list[str] | None = None) -> None:
        """Mirror a built-in memory write to the provider's backend."""

    async def on_delegation(
        self,
        *,
        task: str,
        result: str,
        child_session_key: str = "",
    ) -> None:
        """Observe a sub-agent's task + result on the parent provider."""


# ─── Builtin wrapper around MemoryRetriever ──────────────────────────


class BuiltinMemoryProvider(MemoryProvider):
    """Default provider — wraps the existing ``MemoryRetriever`` so the
    ABC is the canonical path through PiAgent.

    The system_prompt_block emits a frozen recall snapshot per session
    (same semantics as ``PiAgent._ensure_recall_snapshot``): captured
    once on first call, byte-stable across mid-session writes.
    """

    name = "builtin"  # type: ignore[assignment]

    def __init__(
        self,
        retriever: Any,  # MemoryRetriever — Any keeps the import light
        *,
        snapshot_top_k: int = 5,
    ) -> None:
        self._retriever = retriever
        self._snapshot_top_k = snapshot_top_k
        self._snapshots: dict[str, str] = {}

    @property
    def retriever(self) -> Any:
        return self._retriever

    async def initialize(self, session_key: str) -> None:
        # Nothing to do — MemoryRetriever is constructed eagerly.
        return None

    async def system_prompt_block(self, session_key: str) -> str:
        cached = self._snapshots.get(session_key)
        if cached is not None:
            return cached
        block = ""
        try:
            from oxenclaw.memory.hybrid import HybridConfig
            from oxenclaw.memory.retriever import format_memories_for_prompt
            from oxenclaw.memory.temporal_decay import TemporalDecayConfig

            hits = await self._retriever.search(
                query="user identity preferences personal facts",
                k=self._snapshot_top_k,
                hybrid=HybridConfig(enabled=True),
                temporal_decay=TemporalDecayConfig(enabled=True),
            )
            block = format_memories_for_prompt(hits) if hits else ""
        except Exception:
            logger.exception("builtin provider snapshot probe failed")
            block = ""
        self._snapshots[session_key] = block
        return block

    def invalidate_snapshot(self, session_key: str | None = None) -> None:
        if session_key is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(session_key, None)

    async def prefetch(self, session_key: str, query: str) -> str:
        if not query.strip():
            return ""
        try:
            from oxenclaw.memory.hybrid import HybridConfig
            from oxenclaw.memory.retriever import format_memories_as_prelude
            from oxenclaw.memory.temporal_decay import TemporalDecayConfig

            hits = await self._retriever.search(
                query=query,
                k=self._snapshot_top_k,
                hybrid=HybridConfig(enabled=True),
                temporal_decay=TemporalDecayConfig(enabled=True),
            )
            return format_memories_as_prelude(hits) if hits else ""
        except Exception:
            logger.exception("builtin provider prefetch failed")
            return ""

    def get_tool_schemas(self) -> list[Tool]:
        from oxenclaw.memory.tools import (
            memory_get_tool,
            memory_save_tool,
            memory_search_tool,
        )

        return [
            memory_save_tool(self._retriever),
            memory_search_tool(self._retriever),
            memory_get_tool(self._retriever),
        ]

    async def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
        # Built-in tools are delivered via get_tool_schemas() — the
        # PiAgent run loop dispatches them through ToolRegistry.
        # handle_tool_call is reserved for providers that route tool
        # calls themselves.
        raise NotImplementedError(
            "BuiltinMemoryProvider tools are dispatched via ToolRegistry"
        )

    async def on_memory_write(self, text: str, tags: list[str] | None = None) -> None:
        # Built-in writes go directly through MemoryRetriever.save —
        # this hook is for *external* providers to observe the write.
        # No-op for the builtin path.
        return None


# ─── Registry ─────────────────────────────────────────────────────────


class MemoryProviderRegistry:
    """Tracks the active providers.

    Built-in is always first and not removable. At most one *external*
    provider can be registered at a time — registering a second one
    raises ValueError. ``route_tool_call(name)`` resolves a tool name
    to the owning provider for dispatch.
    """

    def __init__(self) -> None:
        self._providers: list[MemoryProvider] = []
        self._has_external: bool = False
        # name -> provider lookup for fast tool_name routing.
        self._tool_owner: dict[str, MemoryProvider] = {}

    def register(self, provider: MemoryProvider, *, external: bool = False) -> None:
        if external and self._has_external:
            existing = next(
                (p for p in self._providers if p is not None and p.name != "builtin"),
                None,
            )
            existing_name = existing.name if existing else "?"
            raise ValueError(
                f"another external memory provider is already registered: "
                f"{existing_name!r}. Only one external provider may run at a "
                "time; unregister the existing one first."
            )
        self._providers.append(provider)
        if external:
            self._has_external = True
        # Index this provider's tool names for routing.
        try:
            schemas = provider.get_tool_schemas()
        except Exception:
            logger.exception("get_tool_schemas raised for provider %r", provider.name)
            schemas = []
        for tool in schemas:
            self._tool_owner[tool.name] = provider

    @property
    def has_external(self) -> bool:
        return self._has_external

    @property
    def providers(self) -> list[MemoryProvider]:
        return list(self._providers)

    def route_tool_call(self, name: str) -> MemoryProvider | None:
        return self._tool_owner.get(name)

    async def on_pre_compress(self, messages: list[Any]) -> dict[str, Any]:
        """Aggregate ``on_pre_compress`` outputs from all providers.

        Returns ``{"insights": [...]}`` flattened across providers so
        the compactor pipeline can prefix the summariser prompt.
        """
        all_insights: list[Any] = []
        for p in self._providers:
            try:
                contribution = await p.on_pre_compress(messages)
            except Exception:
                logger.exception("on_pre_compress raised for provider %r", p.name)
                continue
            if not contribution:
                continue
            if isinstance(contribution, dict):
                ins = contribution.get("insights") or []
                if isinstance(ins, list):
                    all_insights.extend(ins)
        return {"insights": all_insights} if all_insights else {}

    async def on_memory_write(
        self, text: str, tags: list[str] | None = None
    ) -> None:
        """Notify all providers (excluding the built-in originator)."""
        for p in self._providers:
            if p.name == "builtin":
                continue
            try:
                await p.on_memory_write(text, tags)
            except Exception:
                logger.exception(
                    "on_memory_write raised for provider %r", p.name
                )


__all__ = [
    "BuiltinMemoryProvider",
    "MemoryProvider",
    "MemoryProviderRegistry",
]
