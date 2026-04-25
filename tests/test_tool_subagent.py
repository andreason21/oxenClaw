"""Phase T2: subagents tool tests."""

from __future__ import annotations

import asyncio

import sampyclaw.pi.providers  # noqa: F401  registers wrappers

from sampyclaw.pi import (
    InMemoryAuthStorage,
    Model,
    StopEvent,
    TextDeltaEvent,
    register_provider_stream,
)
from sampyclaw.pi.run import RuntimeConfig
from sampyclaw.tools_pkg.subagent import SubagentConfig, subagents_tool


def _model(provider: str) -> Model:
    return Model(
        id=f"m-{provider}",
        provider=provider,
        max_output_tokens=128,
        extra={"base_url": "http://test-fake"},
    )


def _auth(provider: str) -> InMemoryAuthStorage:
    return InMemoryAuthStorage({provider: "k"})  # type: ignore[dict-item]


async def test_subagent_runs_one_turn_and_returns_text() -> None:
    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="child says: ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("subagent_p1", fake)
    cfg = SubagentConfig(model=_model("subagent_p1"), auth=_auth("subagent_p1"))
    tool = subagents_tool(cfg)
    out = await tool.execute({"task": "say ok"})
    assert "child says: ok" in out


async def test_subagent_includes_context_in_user_prompt() -> None:
    seen_messages: list = []

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        seen_messages.append(ctx.messages)
        yield TextDeltaEvent(delta="seen")
        yield StopEvent(reason="end_turn")

    register_provider_stream("subagent_p2", fake)
    cfg = SubagentConfig(model=_model("subagent_p2"), auth=_auth("subagent_p2"))
    tool = subagents_tool(cfg)
    await tool.execute({"task": "summarise", "context": "background facts"})
    assert seen_messages
    user_text = seen_messages[0][0].content  # type: ignore[union-attr]
    assert "Task: summarise" in user_text
    assert "Context:" in user_text
    assert "background facts" in user_text


async def test_subagent_recursion_capped_at_max_depth() -> None:
    """Inside the child, calling `subagents` again must refuse once the
    cap is hit. We simulate by manually building the tool at depth=max."""
    cfg = SubagentConfig(
        model=_model("subagent_p3"), auth=_auth("subagent_p3"), max_depth=1
    )
    # Build the tool already at depth=1; first call should refuse.
    tool = subagents_tool(cfg, current_depth=1)
    out = await tool.execute({"task": "anything"})
    assert "refused" in out
    assert "max_depth=1" in out


async def test_subagent_passes_shared_tools_to_child() -> None:
    """The child's stream wrapper sees the curated tool list."""
    seen_tools: list = []

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        seen_tools.append([t.name for t in ctx.tools])
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("subagent_p4", fake)

    from sampyclaw.tools_pkg.web import web_search_tool

    shared = [web_search_tool()]
    cfg = SubagentConfig(
        model=_model("subagent_p4"),
        auth=_auth("subagent_p4"),
        tools=shared,
    )
    tool = subagents_tool(cfg)
    await tool.execute({"task": "do it"})
    # Child saw web_search + a depth+1 subagents (allowed for grandchild fanout).
    names = seen_tools[0]
    assert "web_search" in names
    assert "subagents" in names
