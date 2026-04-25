"""Phase 3: provider stream wrappers — payload shaping + SSE event translation.

Network calls are stubbed at the aiohttp seam; tests assert on the *event
sequence* the wrapper yields and on the *payload* it would have sent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import sampyclaw.pi.providers  # registers all wrappers  # noqa: F401
from sampyclaw.pi import (
    Api,
    Context,
    Model,
    SimpleStreamOptions,
    SystemMessage,
    TextContent,
    ThinkingBlock,
    ThinkingLevel,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
    assistant_text,
    get_provider_stream,
    text_message,
)
from sampyclaw.pi.providers.anthropic import build_anthropic_payload
from sampyclaw.pi.providers.bedrock import is_anthropic_bedrock_model
from sampyclaw.pi.providers.google import build_google_payload
from sampyclaw.pi.providers._openai_shared import build_openai_payload
from sampyclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)


# ─── Registration ────────────────────────────────────────────────────


def test_all_expected_providers_registered() -> None:
    for pid in [
        "openai",
        "openai-compatible",
        "ollama",
        "vllm",
        "lmstudio",
        "llamacpp",
        "litellm",
        "proxy",
        "groq",
        "deepseek",
        "mistral",
        "together",
        "fireworks",
        "kilocode",
        "anthropic",
        "anthropic-vertex",
        "google",
        "vertex-ai",
        "bedrock",
        "openrouter",
        "moonshot",
        "zai",
        "minimax",
    ]:
        assert get_provider_stream(pid) is not None, pid


# ─── OpenAI payload shaping ─────────────────────────────────────────


def test_openai_payload_lifts_system_into_messages() -> None:
    model = Model(id="gpt-4o", provider="openai")
    api = Api(base_url="https://api.openai.com/v1", api_key="sk-x")
    ctx = Context(
        model=model,
        api=api,
        system="be brief",
        messages=[text_message("hi")],
    )
    payload = build_openai_payload(ctx, stream=True)
    assert payload["messages"][0] == {"role": "system", "content": "be brief"}
    assert payload["messages"][1]["role"] == "user"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}


def test_openai_payload_serializes_tool_use_and_result() -> None:
    model = Model(id="gpt-4o", provider="openai")
    api = Api(base_url="https://api.openai.com/v1", api_key="sk-x")
    asst = ToolUseBlock(id="t1", name="echo", input={"x": 1})
    from sampyclaw.pi.messages import AssistantMessage as A
    ctx = Context(
        model=model,
        api=api,
        messages=[
            text_message("hi"),
            A(content=[asst], stop_reason="tool_use"),
            ToolResultMessage(
                results=[ToolResultBlock(tool_use_id="t1", content="x=1")]
            ),
        ],
    )
    payload = build_openai_payload(ctx, stream=True)
    asst_msg = payload["messages"][1]
    assert asst_msg["role"] == "assistant"
    assert asst_msg["tool_calls"][0]["id"] == "t1"
    args = json.loads(asst_msg["tool_calls"][0]["function"]["arguments"])
    assert args == {"x": 1}
    tool_msg = payload["messages"][2]
    assert tool_msg == {"role": "tool", "tool_call_id": "t1", "content": "x=1"}


def test_ollama_max_tokens_aliased_to_num_predict() -> None:
    model = Model(id="qwen2.5:7b-instruct", provider="ollama")
    api = Api(base_url="http://127.0.0.1:11434/v1")
    ctx = Context(model=model, api=api, max_tokens=512, messages=[text_message("hi")])
    payload = build_openai_payload(ctx, stream=False)
    assert payload["max_tokens"] == 512
    assert payload["num_predict"] == 512


# ─── Anthropic payload shaping ──────────────────────────────────────


def test_anthropic_payload_lifts_system_to_top_field() -> None:
    model = Model(
        id="claude-sonnet-4-6",
        provider="anthropic",
        supports_prompt_cache=True,
        max_output_tokens=1024,
    )
    api = Api(base_url="https://api.anthropic.com", api_key="sk-x")
    ctx = Context(
        model=model,
        api=api,
        system="be brief",
        messages=[text_message("hi")],
        cache_control_breakpoints=4,
    )
    payload = build_anthropic_payload(ctx)
    # system is moved to a list with a cache_control marker.
    assert isinstance(payload["system"], list)
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["messages"][0]["role"] == "user"
    # Last user gets a cache_control marker too.
    last_user = payload["messages"][-1]
    assert last_user["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_anthropic_payload_thinking_budget() -> None:
    model = Model(
        id="claude-sonnet-4-6",
        provider="anthropic",
        supports_thinking=True,
        max_output_tokens=1024,
    )
    api = Api(base_url="https://api.anthropic.com")
    ctx = Context(
        model=model,
        api=api,
        messages=[text_message("hi")],
        thinking="medium",
    )
    payload = build_anthropic_payload(ctx)
    assert payload["thinking"]["type"] == "enabled"
    assert payload["thinking"]["budget_tokens"] == 4096


def test_anthropic_payload_serializes_tool_result_message() -> None:
    model = Model(id="claude-sonnet-4-6", provider="anthropic", max_output_tokens=1024)
    api = Api(base_url="https://api.anthropic.com")
    ctx = Context(
        model=model,
        api=api,
        messages=[
            text_message("hi"),
            ToolResultMessage(
                results=[
                    ToolResultBlock(tool_use_id="t1", content="ok"),
                    ToolResultBlock(tool_use_id="t2", content="boom", is_error=True),
                ]
            ),
        ],
    )
    payload = build_anthropic_payload(ctx)
    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][1]["is_error"] is True


# ─── Google payload shaping ─────────────────────────────────────────


def test_google_payload_lifts_system_and_renames_assistant_role() -> None:
    model = Model(id="gemini-2.5-pro", provider="google")
    api = Api(base_url="https://generativelanguage.googleapis.com", api_key="k")
    from sampyclaw.pi.messages import AssistantMessage as A

    ctx = Context(
        model=model,
        api=api,
        system="be brief",
        messages=[
            text_message("hi"),
            A(content=[TextContent(text="hey")], stop_reason="end_turn"),
        ],
    )
    payload, sys = build_google_payload(ctx)
    assert sys == "be brief"
    assert payload["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][1]["role"] == "model"


def test_google_payload_thinking_budget_applied() -> None:
    model = Model(id="gemini-2.5-pro", provider="google", supports_thinking=True)
    api = Api(base_url="https://generativelanguage.googleapis.com")
    ctx = Context(
        model=model,
        api=api,
        messages=[text_message("hi")],
        thinking="high",
    )
    payload, _ = build_google_payload(ctx)
    assert payload["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 8192


# ─── Bedrock model-id detection ─────────────────────────────────────


def test_bedrock_anthropic_detection() -> None:
    assert is_anthropic_bedrock_model("anthropic.claude-3-sonnet")
    assert is_anthropic_bedrock_model("us.anthropic.claude-haiku-20240307-v1:0")
    assert is_anthropic_bedrock_model("claude-sonnet-4-6")
    assert not is_anthropic_bedrock_model("meta.llama3-1-70b-instruct-v1:0")


# ─── SSE event translation (mocked transport) ───────────────────────


class _FakeContent:
    """Mimic aiohttp StreamReader: async iter over bytes lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def _gen():
            for ln in self._lines:
                yield ln.encode("utf-8")
        return _gen()


class _FakeResp:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self.status = status
        self.content = _FakeContent(lines)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    async def text(self) -> str:
        return ""


class _FakeSession:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self._lines = lines
        self._status = status

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    def post(self, *a, **k):  # type: ignore[no-untyped-def]
        return _FakeResp(self._lines, self._status)


@pytest.fixture
def patch_aiohttp(monkeypatch):  # type: ignore[no-untyped-def]
    """Replace `aiohttp.ClientSession` in a target module with a fake."""

    def _apply(module, lines: list[str], status: int = 200):  # type: ignore[no-untyped-def]
        monkeypatch.setattr(
            module, "aiohttp", _FakeAiohttpModule(lines, status)
        )

    return _apply


class _FakeAiohttpModule:
    def __init__(self, lines: list[str], status: int) -> None:
        self._lines = lines
        self._status = status
        self.ClientConnectionError = ConnectionError

        class _ClientTimeout:
            def __init__(self, total=None):  # type: ignore[no-untyped-def]
                self.total = total

        self.ClientTimeout = _ClientTimeout

    def ClientSession(self, *a, **k):  # type: ignore[no-untyped-def]
        return _FakeSession(self._lines, self._status)


async def test_openai_sse_translates_text_and_tool_deltas(patch_aiohttp) -> None:
    from sampyclaw.pi.providers import _openai_shared as shared

    chunks = [
        'data: {"choices":[{"delta":{"content":"he"}}]}',
        'data: {"choices":[{"delta":{"content":"llo"}}]}',
        (
            'data: {"choices":[{"delta":{"tool_calls":'
            '[{"index":0,"id":"t1","function":{"name":"echo","arguments":""}}]}}]}'
        ),
        (
            'data: {"choices":[{"delta":{"tool_calls":'
            '[{"index":0,"function":{"arguments":"{\\"x\\":1}"}}]}}]}'
        ),
        'data: {"choices":[{"finish_reason":"tool_calls"}]}',
        'data: {"usage":{"prompt_tokens":10,"completion_tokens":5}}',
        "data: [DONE]",
    ]
    patch_aiohttp(shared, chunks)

    model = Model(id="gpt-4o", provider="openai")
    api = Api(base_url="https://api.openai.com/v1", api_key="sk-x")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = []
    async for ev in shared.stream_openai_compatible(ctx, SimpleStreamOptions()):
        events.append(ev)

    text = "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))
    assert text == "hello"
    starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    assert starts and starts[0].name == "echo"
    inputs = "".join(
        e.input_delta for e in events if isinstance(e, ToolUseInputDeltaEvent)
    )
    assert inputs == '{"x":1}'
    assert any(isinstance(e, ToolUseEndEvent) for e in events)
    assert any(isinstance(e, StopEvent) for e in events)


async def test_anthropic_sse_translates_thinking_text_and_tool(
    patch_aiohttp,
) -> None:
    from sampyclaw.pi.providers import anthropic as ant

    chunks = [
        (
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"thinking"}}'
        ),
        (
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"thinking_delta","thinking":"hmm"}}'
        ),
        (
            'data: {"type":"content_block_start","index":1,'
            '"content_block":{"type":"text"}}'
        ),
        (
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"text_delta","text":"hi"}}'
        ),
        (
            'data: {"type":"content_block_start","index":2,'
            '"content_block":{"type":"tool_use","id":"t1","name":"echo"}}'
        ),
        (
            'data: {"type":"content_block_delta","index":2,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"x\\":1}"}}'
        ),
        'data: {"type":"content_block_stop","index":2}',
        (
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
            '"usage":{"input_tokens":10,"output_tokens":4}}'
        ),
        'data: {"type":"message_stop"}',
    ]
    patch_aiohttp(ant, chunks)

    model = Model(
        id="claude-sonnet-4-6",
        provider="anthropic",
        max_output_tokens=1024,
        supports_thinking=True,
    )
    api = Api(base_url="https://api.anthropic.com", api_key="sk-x")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = []
    async for ev in ant.stream_anthropic(ctx, SimpleStreamOptions()):
        events.append(ev)

    from sampyclaw.pi.streaming import ThinkingDeltaEvent

    assert any(isinstance(e, ThinkingDeltaEvent) for e in events)
    assert "".join(
        e.delta for e in events if isinstance(e, TextDeltaEvent)
    ) == "hi"
    starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    assert starts and starts[0].name == "echo"
    assert any(isinstance(e, UsageEvent) for e in events)
    stops = [e for e in events if isinstance(e, StopEvent)]
    assert stops and stops[0].reason == "tool_use"


async def test_google_sse_translates_function_call(patch_aiohttp) -> None:
    from sampyclaw.pi.providers import google as goo

    chunks = [
        (
            'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]},'
            '"finishReason":"STOP"}],"usageMetadata":{"totalTokenCount":5}}'
        ),
    ]
    patch_aiohttp(goo, chunks)

    model = Model(id="gemini-2.5-pro", provider="google")
    api = Api(base_url="https://generativelanguage.googleapis.com", api_key="k")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = []
    async for ev in goo.stream_google(ctx, SimpleStreamOptions()):
        events.append(ev)
    assert any(isinstance(e, TextDeltaEvent) and e.delta == "hi" for e in events)
    assert any(isinstance(e, StopEvent) for e in events)


async def test_openai_http_error_emits_retryable_event(patch_aiohttp) -> None:
    from sampyclaw.pi.providers import _openai_shared as shared
    from sampyclaw.pi.streaming import ErrorEvent

    patch_aiohttp(shared, [], status=503)
    model = Model(id="gpt-4o", provider="openai")
    api = Api(base_url="https://api.openai.com/v1", api_key="sk-x")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = [
        ev async for ev in shared.stream_openai_compatible(ctx, SimpleStreamOptions())
    ]
    assert any(isinstance(e, ErrorEvent) and e.retryable for e in events)
