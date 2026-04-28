"""MemoryProvider ABC, BuiltinMemoryProvider, and registry tests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.memory.provider import (
    BuiltinMemoryProvider,
    MemoryProvider,
    MemoryProviderRegistry,
)

# ─── Fixtures / helpers ───────────────────────────────────────────────


class _StubArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., description="x")


class _StubExternalProvider(MemoryProvider):
    """Minimal external provider that exposes one tool and tracks hooks."""

    name = "stub_external"  # type: ignore[assignment]

    def __init__(self) -> None:
        self.initialized: list[str] = []
        self.shutdown_called: bool = False
        self.pre_compress_args: list[list[Any]] = []
        self.memory_writes: list[tuple[str, list[str] | None]] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []

    async def initialize(self, session_key: str) -> None:
        self.initialized.append(session_key)

    async def system_prompt_block(self, session_key: str) -> str:
        return "<external/>"

    async def prefetch(self, session_key: str, query: str) -> str:
        return "external recall"

    def get_tool_schemas(self) -> list[Tool]:
        async def _h(args: _StubArgs) -> str:
            return f"echoed: {args.text}"

        return [
            FunctionTool(
                name="external_save",
                description="Save into external store",
                input_model=_StubArgs,
                handler=_h,
            )
        ]

    async def handle_tool_call(self, name: str, args: dict[str, Any]) -> str:
        self.tool_calls.append((name, args))
        return f"handled by {self.name}"

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def on_pre_compress(self, messages: list[Any]) -> dict[str, Any]:
        self.pre_compress_args.append(list(messages))
        return {"insights": [f"saw {len(messages)} messages"]}

    async def on_memory_write(self, text: str, tags: list[str] | None = None) -> None:
        self.memory_writes.append((text, tags))


class _FakeRetriever:
    """Stand-in for MemoryRetriever — only the methods Builtin needs."""

    def __init__(self) -> None:
        self.searched: list[str] = []
        self.saves: list[tuple[str, list[str] | None]] = []

    async def search(self, query: str, **kwargs: Any) -> list:
        self.searched.append(query)
        return []  # no hits — keeps the snapshot stable as ""

    async def save(self, text: str, *, tags: list[str] | None = None) -> Any:
        self.saves.append((text, tags))

        class _Report:
            added = 0
            changed = 0
            chunks_embedded = 0

        return _Report()

    @property
    def inbox_path(self) -> Any:
        class _P:
            name = "inbox.md"

        return _P()


# ─── ABC contract ─────────────────────────────────────────────────────


def test_memory_provider_abstract_methods_required() -> None:
    with pytest.raises(TypeError):
        MemoryProvider()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_default_methods_are_no_ops() -> None:
    class _Min(MemoryProvider):
        name = "min"  # type: ignore[assignment]

        async def initialize(self, session_key: str) -> None:
            return None

        def get_tool_schemas(self) -> list[Tool]:
            return []

    p = _Min()
    assert await p.system_prompt_block("s") == ""
    assert await p.prefetch("s", "q") == ""
    await p.sync_turn("s", "u", "a")  # no-op
    await p.shutdown()
    contribution = await p.on_pre_compress([])
    assert contribution == {}


# ─── BuiltinMemoryProvider ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_builtin_wraps_retriever_and_caches_snapshot() -> None:
    retriever = _FakeRetriever()
    p = BuiltinMemoryProvider(retriever)
    await p.initialize("session-a")
    block_1 = await p.system_prompt_block("session-a")
    block_2 = await p.system_prompt_block("session-a")
    # Snapshot byte-stable across calls.
    assert block_1 == block_2
    # Underlying retriever was searched once for the snapshot probe.
    assert len(retriever.searched) == 1


@pytest.mark.asyncio
async def test_builtin_invalidate_snapshot_re_probes() -> None:
    retriever = _FakeRetriever()
    p = BuiltinMemoryProvider(retriever)
    await p.system_prompt_block("s1")
    p.invalidate_snapshot("s1")
    await p.system_prompt_block("s1")
    # Two probes total — invalidate forces a refresh.
    assert len(retriever.searched) == 2


def test_builtin_exposes_three_tools() -> None:
    retriever = _FakeRetriever()
    p = BuiltinMemoryProvider(retriever)
    schemas = p.get_tool_schemas()
    names = sorted(t.name for t in schemas)
    assert names == ["memory_get", "memory_save", "memory_search"]


# ─── MemoryProviderRegistry ──────────────────────────────────────────


def test_registry_accepts_builtin_plus_one_external() -> None:
    reg = MemoryProviderRegistry()
    builtin = BuiltinMemoryProvider(_FakeRetriever())
    ext = _StubExternalProvider()
    reg.register(builtin)
    reg.register(ext, external=True)
    assert reg.has_external is True
    assert [p.name for p in reg.providers] == ["builtin", "stub_external"]


def test_registry_rejects_second_external() -> None:
    reg = MemoryProviderRegistry()
    reg.register(BuiltinMemoryProvider(_FakeRetriever()))
    reg.register(_StubExternalProvider(), external=True)
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_StubExternalProvider(), external=True)


def test_registry_routes_tool_call_to_external() -> None:
    reg = MemoryProviderRegistry()
    builtin = BuiltinMemoryProvider(_FakeRetriever())
    ext = _StubExternalProvider()
    reg.register(builtin)
    reg.register(ext, external=True)
    owner = reg.route_tool_call("external_save")
    assert owner is ext
    # Built-in tools are also routable.
    owner_builtin = reg.route_tool_call("memory_save")
    assert owner_builtin is builtin


@pytest.mark.asyncio
async def test_registry_on_pre_compress_aggregates_insights() -> None:
    reg = MemoryProviderRegistry()
    reg.register(BuiltinMemoryProvider(_FakeRetriever()))
    ext = _StubExternalProvider()
    reg.register(ext, external=True)
    out = await reg.on_pre_compress(["m1", "m2", "m3"])
    assert out == {"insights": ["saw 3 messages"]}
    # External provider received the messages list.
    assert ext.pre_compress_args == [["m1", "m2", "m3"]]


@pytest.mark.asyncio
async def test_registry_on_memory_write_skips_builtin_originator() -> None:
    reg = MemoryProviderRegistry()
    reg.register(BuiltinMemoryProvider(_FakeRetriever()))
    ext = _StubExternalProvider()
    reg.register(ext, external=True)
    await reg.on_memory_write("hello", ["tag1"])
    # External got the mirror; builtin did not (it's the originator).
    assert ext.memory_writes == [("hello", ["tag1"])]


@pytest.mark.asyncio
async def test_registry_on_pre_compress_swallows_provider_errors() -> None:
    reg = MemoryProviderRegistry()

    class _Crashy(MemoryProvider):
        name = "crashy"  # type: ignore[assignment]

        async def initialize(self, session_key: str) -> None:
            return None

        def get_tool_schemas(self) -> list[Tool]:
            return []

        async def on_pre_compress(self, messages: list[Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

    reg.register(_Crashy(), external=True)
    out = await reg.on_pre_compress(["m"])
    assert out == {}  # crash absorbed
