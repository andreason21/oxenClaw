"""Provider stream wrappers — payload shaping + SSE event translation.

oxenClaw's catalog is on-host only (cloud providers were removed
2026-04-29). The remaining tests cover the OpenAI-shape payload
builder (shared by vllm/lmstudio/llamacpp/llamacpp-direct), the
ollama-specific `num_predict` aliasing, and the SSE event-translation
core in `_openai_shared`.

Network calls are stubbed at the aiohttp seam; tests assert on the
*event sequence* the wrapper yields and on the *payload* it would
have sent.
"""

from __future__ import annotations

import pytest

import oxenclaw.pi.providers  # registers all wrappers  # noqa: F401
from oxenclaw.pi import (
    Api,
    Context,
    Model,
    SimpleStreamOptions,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    get_provider_stream,
    text_message,
)
from oxenclaw.pi.providers._openai_shared import build_openai_payload
from oxenclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)

# ─── Registration ────────────────────────────────────────────────────


def test_all_expected_providers_registered() -> None:
    """Catalog providers — exactly five, all on-host."""
    for pid in [
        "ollama",
        "llamacpp-direct",
        "llamacpp",
        "vllm",
        "lmstudio",
    ]:
        assert get_provider_stream(pid) is not None, pid


# ─── OpenAI-shape payload shaping (used by vllm/lmstudio/llamacpp/llamacpp-direct) ──


def test_openai_payload_lifts_system_into_messages() -> None:
    model = Model(id="qwen3.5:9b", provider="vllm")
    api = Api(base_url="http://127.0.0.1:8000/v1")
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
    import json

    model = Model(id="qwen3.5:9b", provider="vllm")
    api = Api(base_url="http://127.0.0.1:8000/v1")
    asst = ToolUseBlock(id="t1", name="echo", input={"x": 1})
    from oxenclaw.pi.messages import AssistantMessage as A

    ctx = Context(
        model=model,
        api=api,
        messages=[
            text_message("hi"),
            A(content=[asst], stop_reason="tool_use"),
            ToolResultMessage(results=[ToolResultBlock(tool_use_id="t1", content="x=1")]),
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
    assert payload["num_predict"] == 512


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
        monkeypatch.setattr(module, "aiohttp", _FakeAiohttpModule(lines, status))

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


async def test_openai_shape_translates_text_and_tool_deltas(patch_aiohttp) -> None:
    """vllm / lmstudio / llamacpp / llamacpp-direct all share this
    SSE-translation path. Tested once against the shared module."""
    from oxenclaw.pi.providers import _openai_shared as shared

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

    model = Model(id="qwen3.5:9b", provider="vllm")
    api = Api(base_url="http://127.0.0.1:8000/v1")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = []
    async for ev in shared.stream_openai_compatible(ctx, SimpleStreamOptions()):
        events.append(ev)

    text = "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))
    assert text == "hello"
    starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    assert starts and starts[0].name == "echo"
    inputs = "".join(e.input_delta for e in events if isinstance(e, ToolUseInputDeltaEvent))
    assert inputs == '{"x":1}'
    assert any(isinstance(e, ToolUseEndEvent) for e in events)
    assert any(isinstance(e, StopEvent) for e in events)


async def test_openai_shape_http_error_emits_retryable_event(patch_aiohttp) -> None:
    from oxenclaw.pi.providers import _openai_shared as shared
    from oxenclaw.pi.streaming import ErrorEvent

    patch_aiohttp(shared, [], status=503)

    model = Model(id="qwen3.5:9b", provider="vllm")
    api = Api(base_url="http://127.0.0.1:8000/v1")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    events = []
    async for ev in shared.stream_openai_compatible(ctx, SimpleStreamOptions()):
        events.append(ev)

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors and errors[0].retryable is True
