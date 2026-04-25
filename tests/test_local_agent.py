"""Tests for LocalAgent: OpenAI-compatible chat/completions inference loop.

The `_chat_complete` method is the single network seam; tests monkey-patch it
with a scripted response sequence so no sockets open.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.agents.local_agent import DEFAULT_MODEL, LocalAgent
from sampyclaw.agents.tools import FunctionTool, ToolRegistry
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _response_text(text: str, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ]
    }


def _response_tool_call(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | str,
    content: str | None = None,
) -> dict[str, Any]:
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments)
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _paths(tmp_path) -> SampyclawPaths:  # type: ignore[no-untyped-def]
    p = SampyclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _inbound(text: str = "hello") -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="user-1",
        text=text,
        received_at=0.0,
    )


def _make_agent_with_responses(
    responses: list[dict[str, Any]], tmp_path, **kwargs
):  # type: ignore[no-untyped-def]
    # `_chat_complete` is monkey-patched, so streaming defaults off in tests
    # that don't opt into it. `warmup` defaults off too unless the test wants
    # to assert warmup behavior.
    kwargs.setdefault("warmup", False)
    kwargs.setdefault("stream", False)
    agent = LocalAgent(paths=_paths(tmp_path), **kwargs)
    mock = AsyncMock(side_effect=list(responses))
    agent._chat_complete = mock  # type: ignore[method-assign]
    # Warmup goes through `_chat_complete_once`; share the same mock so the
    # response queue covers warmup pings too.
    agent._chat_complete_once = mock  # type: ignore[method-assign]
    return agent, mock


async def _collect(agent, env):  # type: ignore[no-untyped-def]
    ctx = AgentContext(agent_id=agent.id, session_key="s1")
    return [sp async for sp in agent.handle(env, ctx)]


async def test_defaults_target_tool_capable_ollama_model(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent = LocalAgent(paths=_paths(tmp_path))
    assert agent._base_url.endswith("11434/v1")
    assert agent._model == DEFAULT_MODEL == "gemma4:latest"


async def test_simple_stop_reply(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent, mock = _make_agent_with_responses([_response_text("hello back")], tmp_path)
    outs = await _collect(agent, _inbound("hi"))
    assert len(outs) == 1
    assert outs[0].text == "hello back"
    mock.assert_awaited_once()


async def test_empty_inbound_skips_api(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent, mock = _make_agent_with_responses([_response_text("unused")], tmp_path)
    outs = await _collect(agent, _inbound("   "))
    assert outs == []
    mock.assert_not_awaited()


async def test_first_turn_prepends_system_message(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent, mock = _make_agent_with_responses([_response_text("ok")], tmp_path)
    await _collect(agent, _inbound("hey"))
    call = mock.await_args_list[0].kwargs
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["role"] == "user"


async def test_tools_parameter_uses_openai_format(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _A(BaseModel):
        pass

    async def _h(_: _A) -> str:
        return "ok"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="ping", description="d", input_model=_A, handler=_h))
    agent, mock = _make_agent_with_responses(
        [_response_text("ok")], tmp_path, tools=tools
    )
    await _collect(agent, _inbound("go"))
    call = mock.await_args_list[0].kwargs
    t = call["tools"][0]
    assert t["type"] == "function"
    assert t["function"]["name"] == "ping"


async def test_tools_omitted_from_payload_when_registry_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Empty tools list must not appear in the outgoing HTTP payload — some
    OpenAI-compatible servers reject `tools: []`."""

    class _FakeResponse:
        status = 200

        def __init__(self, data):  # type: ignore[no-untyped-def]
            self._data = data

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def raise_for_status(self):  # type: ignore[no-untyped-def]
            return None

        async def json(self):  # type: ignore[no-untyped-def]
            return self._data

    captured: dict = {}

    class _FakeSession:
        async def close(self) -> None:
            return None

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            captured["payload"] = kwargs.get("json")
            return _FakeResponse(_response_text("ok"))

    agent = LocalAgent(
        paths=_paths(tmp_path),
        http_session=_FakeSession(),  # type: ignore[arg-type]
        warmup=False,
        stream=False,
    )
    await _collect(agent, _inbound("hey"))
    assert "tools" not in captured["payload"]


async def test_tool_call_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _A(BaseModel):
        pass

    async def _h(_: _A) -> str:
        return "2026-04-25T00:00:00+00:00"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="get_time", description="d", input_model=_A, handler=_h
        )
    )

    agent, mock = _make_agent_with_responses(
        [
            _response_tool_call(call_id="c1", name="get_time", arguments={}),
            _response_text("The time is 2026-04-25T00:00:00+00:00."),
        ],
        tmp_path,
        tools=tools,
    )
    outs = await _collect(agent, _inbound("what time?"))
    assert len(outs) == 1
    assert "2026-04-25" in outs[0].text
    assert mock.await_count == 2

    # Second call must include the tool-result role="tool" message.
    second = mock.await_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "c1"
    assert "2026-04-25" in tool_msg["content"]


async def test_missing_tool_reports_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent, mock = _make_agent_with_responses(
        [
            _response_tool_call(call_id="c1", name="unknown", arguments={}),
            _response_text("sorry"),
        ],
        tmp_path,
    )
    await _collect(agent, _inbound("do it"))
    second = mock.await_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg.get("is_error") is True
    assert "unknown" in tool_msg["content"]


async def test_bad_json_arguments_reports_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent, mock = _make_agent_with_responses(
        [
            _response_tool_call(
                call_id="c1", name="get_time", arguments="not json{"
            ),
            _response_text("sorry"),
        ],
        tmp_path,
    )
    await _collect(agent, _inbound("x"))
    second = mock.await_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg.get("is_error") is True


async def test_tool_raises_surface_as_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class _A(BaseModel):
        pass

    async def _boom(_: _A) -> str:
        raise RuntimeError("boom")

    tools = ToolRegistry()
    tools.register(
        FunctionTool(name="boom", description="d", input_model=_A, handler=_boom)
    )
    agent, mock = _make_agent_with_responses(
        [
            _response_tool_call(call_id="c1", name="boom", arguments={}),
            _response_text("k"),
        ],
        tmp_path,
        tools=tools,
    )
    await _collect(agent, _inbound("try"))
    second = mock.await_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second if m["role"] == "tool")
    assert tool_msg.get("is_error") is True
    assert "boom" in tool_msg["content"]


async def test_long_reply_is_chunked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    body = "a" * 10_000
    agent, _ = _make_agent_with_responses(
        [_response_text(body)], tmp_path, chunk_limit=4000
    )
    outs = await _collect(agent, _inbound("x"))
    assert len(outs) >= 3
    assert all(len(o.text or "") <= 4000 for o in outs)


async def test_history_persists_between_turns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _paths(tmp_path)
    agent = LocalAgent(paths=paths)
    agent._chat_complete = AsyncMock(  # type: ignore[method-assign]
        side_effect=[_response_text("r1"), _response_text("r2")]
    )
    ctx = AgentContext(agent_id=agent.id, session_key="s1")
    _ = [sp async for sp in agent.handle(_inbound("turn1"), ctx)]
    _ = [sp async for sp in agent.handle(_inbound("turn2"), ctx)]

    hist = ConversationHistory(paths.session_file(agent.id, "s1"))
    # system + u1 + a1 + u2 + a2
    assert len(hist) == 5
    assert hist.messages()[0]["role"] == "system"


async def test_api_key_sent_as_bearer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Verify the _chat_complete request would include the auth header.

    We inspect a real `_chat_complete` call via an aiohttp session mock.
    """
    import aiohttp

    class _FakeResponse:
        status = 200

        def __init__(self, data):  # type: ignore[no-untyped-def]
            self._data = data

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def raise_for_status(self):  # type: ignore[no-untyped-def]
            return None

        async def json(self):  # type: ignore[no-untyped-def]
            return self._data

    captured: dict = {}

    class _FakeSession:
        async def close(self) -> None:
            return None

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["payload"] = kwargs.get("json")
            return _FakeResponse(_response_text("ok"))

    agent = LocalAgent(
        paths=_paths(tmp_path),
        api_key="secret",
        http_session=_FakeSession(),  # type: ignore[arg-type]
        warmup=False,
        stream=False,
    )
    await _collect(agent, _inbound("hi"))
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["payload"]["model"] == DEFAULT_MODEL


async def test_max_iterations_bail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Server keeps requesting tool use forever; agent must stop after cap.
    class _A(BaseModel):
        pass

    async def _h(_: _A) -> str:
        return "ok"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="x", description="d", input_model=_A, handler=_h))

    responses = [
        _response_tool_call(call_id=f"c{i}", name="x", arguments={})
        for i in range(10)
    ]
    agent, _ = _make_agent_with_responses(
        responses, tmp_path, tools=tools, max_tool_iterations=3
    )
    outs = await _collect(agent, _inbound("do it"))
    assert outs and "max tool iterations" in (outs[0].text or "")


def test_rejects_bad_config(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        LocalAgent(paths=_paths(tmp_path), max_tool_iterations=0)
    with pytest.raises(ValueError):
        LocalAgent(paths=_paths(tmp_path), chunk_limit=0)


async def test_aclose_closes_owned_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agent = LocalAgent(paths=_paths(tmp_path))
    # Force lazy session creation by touching the helper.
    session = await agent._ensure_session()
    assert session is agent._http
    await agent.aclose()
    assert agent._http is None


async def test_aclose_leaves_external_session_open(tmp_path) -> None:  # type: ignore[no-untyped-def]
    external = AsyncMock()
    external.close = AsyncMock()
    agent = LocalAgent(paths=_paths(tmp_path), http_session=external)  # type: ignore[arg-type]
    await agent.aclose()
    external.close.assert_not_awaited()


# ─── New behavior: parallel tools, JSON self-correct, usage, num_predict ───


async def test_parallel_tool_calls_run_concurrently(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Multiple tool_calls in one assistant message must execute via gather."""
    import asyncio as _asyncio

    class _A(BaseModel):
        i: int

    started = _asyncio.Event()
    started_count = {"n": 0}

    async def _slow(args: _A) -> str:
        started_count["n"] += 1
        if started_count["n"] >= 2:
            started.set()
        await started.wait()  # both must have started before either finishes
        return f"done {args.i}"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="slow", description="d", input_model=_A, handler=_slow))

    parallel_call_msg = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "slow", "arguments": '{"i":1}'}},
                        {"id": "c2", "type": "function", "function": {"name": "slow", "arguments": '{"i":2}'}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    final = _response_text("both done")
    agent, _ = _make_agent_with_responses([parallel_call_msg, final], tmp_path, tools=tools)
    outs = await _asyncio.wait_for(_collect(agent, _inbound("go")), timeout=2.0)
    assert outs and "both done" in (outs[0].text or "")
    assert started_count["n"] == 2


async def test_bad_json_args_self_correct(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A tool_call with malformed JSON arguments must surface a tool-error
    that the model can react to in the next turn (no exception)."""

    class _A(BaseModel):
        x: int

    async def _h(args: _A) -> str:
        return f"x={args.x}"

    tools = ToolRegistry()
    tools.register(FunctionTool(name="t", description="d", input_model=_A, handler=_h))

    bad_call = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{not json"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    recovered = _response_text("recovered")
    agent, _ = _make_agent_with_responses([bad_call, recovered], tmp_path, tools=tools)
    outs = await _collect(agent, _inbound("go"))
    assert outs and "recovered" in (outs[0].text or "")
    # Confirm the tool result that was fed back is an is_error message.
    hist = ConversationHistory(agent._paths.session_file(agent.id, "s1"))
    tool_msgs = [m for m in hist.messages() if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0].get("is_error") is True
    assert "not valid JSON" in tool_msgs[0]["content"]


async def test_payload_includes_num_predict_alias(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Ollama's OpenAI shim respects num_predict; ensure we send it alongside max_tokens."""

    class _FakeResponse:
        status = 200

        def __init__(self, data):  # type: ignore[no-untyped-def]
            self._data = data

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        def raise_for_status(self):  # type: ignore[no-untyped-def]
            return None

        async def json(self):  # type: ignore[no-untyped-def]
            return self._data

    captured: dict = {}

    class _FakeSession:
        async def close(self) -> None:
            return None

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            captured["payload"] = kwargs.get("json")
            return _FakeResponse(_response_text("ok"))

    agent = LocalAgent(
        paths=_paths(tmp_path),
        http_session=_FakeSession(),  # type: ignore[arg-type]
        max_tokens=512,
        warmup=False,
        stream=False,
    )
    await _collect(agent, _inbound("hi"))
    assert captured["payload"]["max_tokens"] == 512
    assert captured["payload"]["num_predict"] == 512


async def test_usage_logged(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging as _logging
    caplog.set_level(_logging.INFO, logger="sampyclaw.agents.local")
    response = {
        "choices": [
            {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }
    agent, _ = _make_agent_with_responses([response], tmp_path)
    await _collect(agent, _inbound("hi"))
    assert any("prompt=12" in r.message for r in caplog.records)


async def test_truncation_drops_old_turns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Pre-existing huge history is trimmed before the next request goes out."""
    agent, mock = _make_agent_with_responses([_response_text("ok")], tmp_path, max_history_chars=400)
    # Pre-load a long history file.
    hist = ConversationHistory(agent._paths.session_file(agent.id, "s1"))
    hist.append({"role": "system", "content": "S"})
    for i in range(30):
        hist.append({"role": "user", "content": f"user-{i}-" + "x" * 50})
        hist.append({"role": "assistant", "content": f"asst-{i}-" + "y" * 50})
    hist.save()

    await _collect(agent, _inbound("new"))
    sent_messages = mock.await_args.kwargs["messages"]
    assert sent_messages[0]["role"] == "system"
    # Very first user/asst pair should have been dropped.
    contents = " ".join(
        m.get("content", "") if isinstance(m.get("content"), str) else ""
        for m in sent_messages
    )
    assert "user-0-" not in contents


async def test_warmup_runs_once_before_first_request(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Warmup must call the model exactly once at first user message."""
    responses = [_response_text("warm"), _response_text("real")]
    agent, mock = _make_agent_with_responses(responses, tmp_path, warmup=True, stream=False)
    await _collect(agent, _inbound("hi"))
    assert mock.await_count == 2  # warmup + real
    # Second handle() must NOT re-warmup.
    mock.reset_mock(side_effect=True)
    mock.side_effect = [_response_text("again")]
    await _collect(agent, _inbound("again"))
    assert mock.await_count == 1


async def test_retry_recovers_from_503(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A 503 response must be retried; second attempt success returns OK."""
    import aiohttp as _aiohttp

    class _Resp:
        def __init__(self, status: int, data: dict):
            self.status = status
            self._data = data

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return False

        async def text(self) -> str:
            return "busy"

        def raise_for_status(self) -> None:
            if self.status >= 400:
                raise _aiohttp.ClientResponseError(
                    request_info=None, history=(), status=self.status  # type: ignore[arg-type]
                )

        async def json(self) -> dict:
            return self._data

    queue = [_Resp(503, {}), _Resp(200, _response_text("recovered"))]

    class _Session:
        async def close(self) -> None:
            return None

        def post(self, url, **kwargs):  # type: ignore[no-untyped-def]
            return queue.pop(0)

    agent = LocalAgent(
        paths=_paths(tmp_path),
        http_session=_Session(),  # type: ignore[arg-type]
        warmup=False,
        stream=False,
        backoff_initial=0.0,
        backoff_max=0.0,
    )
    outs = await _collect(agent, _inbound("hi"))
    assert outs and "recovered" in (outs[0].text or "")
