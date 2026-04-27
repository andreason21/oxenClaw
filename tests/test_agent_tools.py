"""Tests for ToolRegistry + FunctionTool + builtin tools."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from oxenclaw.agents.builtin_tools import default_tools, echo_tool, get_time_tool
from oxenclaw.agents.tools import FunctionTool, Tool, ToolRegistry


class _Args(BaseModel):
    value: int


def _double() -> Tool:
    def _h(a: _Args) -> str:
        return str(a.value * 2)

    return FunctionTool(
        name="double",
        description="Double an int.",
        input_model=_Args,
        handler=_h,
    )


def test_function_tool_requires_name() -> None:
    with pytest.raises(ValueError):
        FunctionTool(name="", description="x", input_model=_Args, handler=lambda a: "y")


def test_function_tool_schema_from_pydantic() -> None:
    schema = _double().input_schema
    assert schema["type"] == "object"
    assert "value" in schema["properties"]


async def test_function_tool_execute_sync() -> None:
    assert await _double().execute({"value": 3}) == "6"


async def test_function_tool_execute_async_handler() -> None:
    async def _h(a: _Args) -> str:
        return f"v={a.value}"

    t = FunctionTool(name="async", description="d", input_model=_Args, handler=_h)
    assert await t.execute({"value": 5}) == "v=5"


async def test_function_tool_validation_error_propagates() -> None:
    with pytest.raises(Exception):
        await _double().execute({"value": "not-an-int"})


def test_registry_register_and_get() -> None:
    r = ToolRegistry()
    r.register(_double())
    assert r.get("double") is not None
    assert r.names() == ["double"]
    assert len(r) == 1


def test_registry_rejects_duplicate() -> None:
    r = ToolRegistry()
    r.register(_double())
    with pytest.raises(ValueError):
        r.register(_double())


def test_registry_register_all() -> None:
    r = ToolRegistry()
    r.register_all(default_tools())
    # default_tools() now ships read-only fs primitives alongside the
    # legacy echo/get_time pair. Mutating tools (write/edit/shell/
    # process) are added later by the factory under approval gating;
    # they're not in default_tools() itself.
    expected = {"echo", "get_time", "read_file", "list_dir", "grep", "glob", "read_pdf"}
    assert set(r.names()) == expected


def test_registry_as_anthropic_tools_shape() -> None:
    r = ToolRegistry()
    r.register(_double())
    tools = r.as_anthropic_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["name"] == "double"
    assert t["description"] == "Double an int."
    assert t["input_schema"]["type"] == "object"


def test_registry_as_openai_tools_shape() -> None:
    r = ToolRegistry()
    r.register(_double())
    tools = r.as_openai_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "double"
    assert t["function"]["description"] == "Double an int."
    assert t["function"]["parameters"]["type"] == "object"


def test_registry_as_openai_tools_empty_registry_returns_empty_list() -> None:
    assert ToolRegistry().as_openai_tools() == []


async def test_get_time_tool_returns_iso_utc_string() -> None:
    out = await get_time_tool().execute({})
    # rough shape check — must include T (ISO) and end with +00:00 or Z
    assert "T" in out
    assert out.endswith("+00:00") or out.endswith("Z")


async def test_echo_tool_returns_input_verbatim() -> None:
    assert await echo_tool().execute({"text": "hi"}) == "hi"


async def test_echo_tool_rejects_missing_text() -> None:
    with pytest.raises(Exception):
        await echo_tool().execute({})
