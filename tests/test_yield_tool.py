"""sessions_yield: cooperative early-stop signal."""

from __future__ import annotations

import asyncio

from oxenclaw.tools_pkg.yield_tool import yield_tool


async def test_yield_tool_sets_abort_event() -> None:
    ev = asyncio.Event()
    t = yield_tool(abort_event=ev)
    out = await t.execute({"reason": "waiting on user reply"})
    assert ev.is_set()
    assert "yielded" in out
    assert "waiting on user reply" in out


async def test_yield_tool_without_event_returns_text_only() -> None:
    t = yield_tool()
    out = await t.execute({"reason": "lab test"})
    assert "yielded" in out
    assert "lab test" in out


def test_yield_tool_metadata() -> None:
    t = yield_tool()
    assert t.name == "sessions_yield"
    schema = t.input_schema
    assert "reason" in schema["properties"]
