"""Tests for AnthropicAgent: inference loop, tool execution, history persistence.

The Anthropic client is mocked — every test controls `messages.create` by
feeding scripted responses. We never hit the network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from sampyclaw.agents.anthropic_agent import AnthropicAgent
from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.agents.tools import FunctionTool, ToolRegistry
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id_: str, name: str, input_: dict) -> SimpleNamespace:  # type: ignore[type-arg]
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def _response(stop_reason: str, *blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks))


def _inbound(text: str = "hello") -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="user-1",
        text=text,
        received_at=0.0,
    )


def _client_returning(*responses: SimpleNamespace) -> MagicMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    return client


def _paths(tmp_path) -> SampyclawPaths:  # type: ignore[no-untyped-def]
    p = SampyclawPaths(home=tmp_path)
    p.ensure_home()
    return p


async def _collect(agent: AnthropicAgent, env: InboundEnvelope):  # type: ignore[no-untyped-def]
    ctx = AgentContext(agent_id=agent.id, session_key="s1")
    return [sp async for sp in agent.handle(env, ctx)]


async def test_simple_end_turn_reply(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(_response("end_turn", _text_block("hi there")))
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path))
    outs = await _collect(agent, _inbound("hello"))
    assert len(outs) == 1
    assert outs[0].text == "hi there"
    client.messages.create.assert_awaited_once()


async def test_empty_inbound_yields_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(_response("end_turn", _text_block("unused")))
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path))
    outs = await _collect(agent, _inbound("   "))
    assert outs == []
    client.messages.create.assert_not_awaited()


async def test_single_tool_call_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _Args(BaseModel):
        pass

    async def _handler(_: _Args) -> str:
        return "2026-04-25T00:00:00+00:00"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="get_time",
            description="x",
            input_model=_Args,
            handler=_handler,
        )
    )

    client = _client_returning(
        _response("tool_use", _tool_use_block("t1", "get_time", {})),
        _response("end_turn", _text_block("The time is 2026-04-25T00:00:00+00:00.")),
    )
    agent = AnthropicAgent(client=client, tools=tools, paths=_paths(tmp_path))
    outs = await _collect(agent, _inbound("what time is it?"))
    assert len(outs) == 1
    assert "2026-04-25" in outs[0].text
    assert client.messages.create.await_count == 2


async def test_multiple_tool_calls_in_one_response(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _Args(BaseModel):
        pass

    async def _handler(_: _Args) -> str:
        return "ok"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(name="a", description="d", input_model=_Args, handler=_handler)
    )
    tools.register(
        FunctionTool(name="b", description="d", input_model=_Args, handler=_handler)
    )

    client = _client_returning(
        _response(
            "tool_use",
            _tool_use_block("t1", "a", {}),
            _tool_use_block("t2", "b", {}),
        ),
        _response("end_turn", _text_block("done")),
    )
    agent = AnthropicAgent(client=client, tools=tools, paths=_paths(tmp_path))
    outs = await _collect(agent, _inbound("x"))
    assert outs[0].text == "done"
    # Second create call must include both tool_results in the last user message
    second_call_messages = client.messages.create.await_args_list[1].kwargs["messages"]
    last = second_call_messages[-1]
    assert last["role"] == "user"
    tool_result_ids = [b["tool_use_id"] for b in last["content"] if b.get("type") == "tool_result"]
    assert tool_result_ids == ["t1", "t2"]


async def test_missing_tool_reports_error_in_result(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(
        _response("tool_use", _tool_use_block("t1", "unknown", {})),
        _response("end_turn", _text_block("sorry")),
    )
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path))
    await _collect(agent, _inbound("do it"))
    second_messages = client.messages.create.await_args_list[1].kwargs["messages"]
    tool_result = second_messages[-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "unknown" in tool_result["content"]


async def test_tool_raises_surface_as_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _Args(BaseModel):
        pass

    async def _boom(_: _Args) -> str:
        raise RuntimeError("kaboom")

    tools = ToolRegistry()
    tools.register(
        FunctionTool(name="boom", description="d", input_model=_Args, handler=_boom)
    )
    client = _client_returning(
        _response("tool_use", _tool_use_block("t1", "boom", {})),
        _response("end_turn", _text_block("k")),
    )
    agent = AnthropicAgent(client=client, tools=tools, paths=_paths(tmp_path))
    await _collect(agent, _inbound("try it"))
    second_messages = client.messages.create.await_args_list[1].kwargs["messages"]
    tool_result = second_messages[-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "kaboom" in tool_result["content"]


async def test_long_reply_is_chunked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    long_body = "a" * 10_000
    client = _client_returning(_response("end_turn", _text_block(long_body)))
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path), chunk_limit=4000)
    outs = await _collect(agent, _inbound("x"))
    assert len(outs) >= 3
    assert all(len(o.text or "") <= 4000 for o in outs)


async def test_history_is_persisted_between_calls(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(
        _response("end_turn", _text_block("first reply")),
        _response("end_turn", _text_block("second reply")),
    )
    paths = _paths(tmp_path)
    agent = AnthropicAgent(client=client, paths=paths)
    ctx = AgentContext(agent_id=agent.id, session_key="s1")

    _ = [sp async for sp in agent.handle(_inbound("turn 1"), ctx)]
    _ = [sp async for sp in agent.handle(_inbound("turn 2"), ctx)]

    hist = ConversationHistory(paths.session_file(agent.id, "s1"))
    # turn1 user + turn1 assistant + turn2 user + turn2 assistant
    assert len(hist) == 4
    assert hist.messages()[0]["content"] == "turn 1"
    assert hist.messages()[2]["content"] == "turn 2"


async def test_system_prompt_carries_cache_control(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(_response("end_turn", _text_block("hi")))
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path))
    await _collect(agent, _inbound("hey"))
    call = client.messages.create.await_args_list[0].kwargs
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}


async def test_tools_omitted_when_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    client = _client_returning(_response("end_turn", _text_block("hi")))
    agent = AnthropicAgent(client=client, paths=_paths(tmp_path))
    await _collect(agent, _inbound("hey"))
    call = client.messages.create.await_args_list[0].kwargs
    assert "tools" not in call


async def test_tools_passed_when_registered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tools = ToolRegistry()
    from sampyclaw.agents.builtin_tools import echo_tool

    tools.register(echo_tool())
    client = _client_returning(_response("end_turn", _text_block("hi")))
    agent = AnthropicAgent(client=client, tools=tools, paths=_paths(tmp_path))
    await _collect(agent, _inbound("hey"))
    call = client.messages.create.await_args_list[0].kwargs
    assert call["tools"][0]["name"] == "echo"


async def test_max_iterations_bail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Server keeps requesting tool use forever; agent must stop after cap.
    responses = [
        _response("tool_use", _tool_use_block(f"t{i}", "echo", {"text": "x"}))
        for i in range(10)
    ]
    from sampyclaw.agents.builtin_tools import echo_tool

    tools = ToolRegistry()
    tools.register(echo_tool())

    client = _client_returning(*responses)
    agent = AnthropicAgent(
        client=client, tools=tools, paths=_paths(tmp_path), max_tool_iterations=3
    )
    outs = await _collect(agent, _inbound("do it"))
    assert client.messages.create.await_count == 3
    assert outs and "max tool iterations" in (outs[0].text or "")


def test_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        AnthropicAgent(max_tool_iterations=0)
    with pytest.raises(ValueError):
        AnthropicAgent(chunk_limit=0)
