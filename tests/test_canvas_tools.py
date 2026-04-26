"""Tests for sampyclaw.tools_pkg.canvas tool factories."""

from __future__ import annotations

import pytest

from sampyclaw.canvas import (
    CanvasEventBus,
    CanvasNotOpenError,
    CanvasResourceCapError,
    CanvasStore,
)
from sampyclaw.tools_pkg.canvas import (
    canvas_eval_tool,
    canvas_hide_tool,
    canvas_present_tool,
    default_canvas_tools,
)


def _bundle() -> tuple[CanvasStore, CanvasEventBus]:
    return CanvasStore(), CanvasEventBus()


@pytest.mark.asyncio
async def test_present_writes_to_store() -> None:
    store, bus = _bundle()
    tool = canvas_present_tool(agent_id="a", store=store, bus=bus)
    out = await tool.execute({"html": "<!doctype html><p>x</p>", "title": "T"})
    assert "presented" in out
    assert store.get("a").title == "T"


@pytest.mark.asyncio
async def test_present_refuses_oversized_html() -> None:
    store, bus = _bundle()
    tool = canvas_present_tool(agent_id="a", store=store, bus=bus, max_html_bytes=128)
    with pytest.raises(CanvasResourceCapError):
        await tool.execute({"html": "x" * 129})


@pytest.mark.asyncio
async def test_hide_marks_state_hidden() -> None:
    store, bus = _bundle()
    canvas_present_tool_t = canvas_present_tool(agent_id="a", store=store, bus=bus)
    await canvas_present_tool_t.execute({"html": "<p>x</p>"})
    hide = canvas_hide_tool(agent_id="a", store=store, bus=bus)
    out = await hide.execute({})
    assert out == "canvas hidden"
    assert store.get("a").hidden is True


@pytest.mark.asyncio
async def test_eval_refuses_without_open_canvas() -> None:
    store, bus = _bundle()
    tool = canvas_eval_tool(agent_id="a", store=store, bus=bus)
    with pytest.raises(CanvasNotOpenError):
        await tool.execute({"expression": "1"})


def test_default_bundle_has_two_tools() -> None:
    store, bus = _bundle()
    tools = default_canvas_tools(agent_id="a", store=store, bus=bus)
    assert {t.name for t in tools} == {"canvas_present", "canvas_hide"}


def test_factory_returns_no_tools_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMPYCLAW_ENABLE_CANVAS", raising=False)
    from sampyclaw.agents.factory import _maybe_canvas_tools

    assert _maybe_canvas_tools("a") == []


def test_factory_returns_tools_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPYCLAW_ENABLE_CANVAS", "1")
    # reset singletons so the test is isolated
    from sampyclaw.agents.factory import _maybe_canvas_tools
    from sampyclaw.canvas import reset_default_canvas

    reset_default_canvas()
    tools = _maybe_canvas_tools("a")
    assert {t.name for t in tools} == {"canvas_present", "canvas_hide"}
    reset_default_canvas()


def test_present_tool_emits_schema() -> None:
    store, bus = _bundle()
    tool = canvas_present_tool(agent_id="a", store=store, bus=bus)
    schema = tool.input_schema
    assert "html" in schema["properties"]
    assert "title" in schema["properties"]
