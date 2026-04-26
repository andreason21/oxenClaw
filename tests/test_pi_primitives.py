"""Phase 1+2: pi-* primitive port — messages, tools, models, registry, auth."""

from __future__ import annotations

import pytest

from sampyclaw.pi import (
    AgentSession,
    AssistantMessage,
    Context,
    CreateAgentSessionOptions,
    EnvAuthStorage,
    InMemoryAuthStorage,
    InMemoryModelRegistry,
    InMemorySessionManager,
    MissingCredential,
    Model,
    SimpleStreamOptions,
    SystemMessage,
    TextContent,
    ThinkingLevel,
    ToolUseBlock,
    UserMessage,
    assistant_text,
    default_registry,
    estimate_tokens,
    estimate_tokens_for_text,
    inline_api,
    is_inline_provider,
    model_context_window,
    normalize_provider_id,
    register_provider_stream,
    resolve_api,
    stream_simple,
    text_message,
)

# ─── messages ─────────────────────────────────────────────────────


def test_assistant_message_with_mixed_content() -> None:
    msg = AssistantMessage(
        content=[
            TextContent(text="here is a tool call"),
            ToolUseBlock(id="t1", name="echo", input={"text": "hi"}),
        ],
        stop_reason="tool_use",
    )
    assert msg.role == "assistant"
    assert msg.stop_reason == "tool_use"
    assert msg.content[1].name == "echo"  # type: ignore[union-attr]


def test_text_message_helpers() -> None:
    u = text_message("hi")
    assert isinstance(u, UserMessage)
    assert u.content == "hi"
    s = text_message("be brief", role="system")
    assert isinstance(s, SystemMessage)


def test_assistant_text_default_stop() -> None:
    a = assistant_text("done")
    assert a.stop_reason == "end_turn"
    assert a.content[0].text == "done"  # type: ignore[union-attr]


# ─── tokens ───────────────────────────────────────────────────────


def test_estimate_tokens_text_grows_with_length() -> None:
    short = estimate_tokens_for_text("hi")
    long = estimate_tokens_for_text("hi " * 1000)
    assert long > short
    # Empty is zero.
    assert estimate_tokens_for_text("") == 0


def test_estimate_tokens_for_message_list_includes_overhead() -> None:
    msgs = [text_message("hi"), assistant_text("hello back")]
    total = estimate_tokens(msgs)
    # Two messages × ~4 overhead + 2 short content tokens minimum.
    assert total >= 8


def test_model_context_window_lookup_and_default() -> None:
    assert model_context_window("claude-sonnet-4-6") == 1_000_000
    assert model_context_window("unknown-model-xyz", default=4096) == 4096


# ─── thinking ─────────────────────────────────────────────────────


def test_thinking_level_enum_round_trip() -> None:
    assert ThinkingLevel("medium") is ThinkingLevel.MEDIUM
    assert ThinkingLevel.OFF.value == "off"


# ─── streaming ────────────────────────────────────────────────────


def test_stream_simple_dispatches_via_registry() -> None:
    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        from sampyclaw.pi.streaming import StopEvent, TextDeltaEvent

        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("ollama", fake_stream)
    model = Model(id="qwen2.5:7b-instruct", provider="ollama")
    api = inline_api(model)
    ctx = Context(model=model, api=api, system="hi", messages=[text_message("hi")])
    out = stream_simple(ctx, SimpleStreamOptions())
    assert out is not None  # async iterator returned


# ─── registry + auth ──────────────────────────────────────────────


def test_provider_id_alias_normalises() -> None:
    assert normalize_provider_id("Claude") == "anthropic"
    assert normalize_provider_id("vertex") == "vertex-ai"
    assert normalize_provider_id("openai") == "openai"  # passthrough
    assert normalize_provider_id("Brand-New-Provider") == "brand-new-provider"


def test_inmemory_registry_aliases_resolve() -> None:
    reg = InMemoryModelRegistry(models=[Model(id="a", provider="ollama", aliases=("a-alias",))])
    assert reg.get("a-alias") is reg.get("a")
    assert len(reg) == 1
    assert reg.by_provider("ollama") == [reg.require("a")]


def test_default_registry_seeds_known_models() -> None:
    reg = default_registry()
    assert reg.get("claude-sonnet-4-6") is not None
    assert reg.get("qwen2.5:7b-instruct") is not None
    # Aliases work.
    assert reg.get("claude-haiku-4-5") is not None


async def test_inline_provider_synthesises_api() -> None:
    model = Model(id="qwen2.5:7b-instruct", provider="ollama")
    api = inline_api(model)
    assert api.base_url.endswith("11434/v1")
    assert api.api_key is None


async def test_inline_provider_extra_overrides_base() -> None:
    model = Model(
        id="local",
        provider="ollama",
        extra={"base_url": "http://10.0.0.5:11434/v1"},
    )
    assert inline_api(model).base_url == "http://10.0.0.5:11434/v1"


def test_is_inline_provider_classifies() -> None:
    assert is_inline_provider("ollama")
    assert is_inline_provider("vllm")
    assert not is_inline_provider("anthropic")
    assert not is_inline_provider("openai")


async def test_resolve_api_inline_skips_auth() -> None:
    auth = InMemoryAuthStorage()  # empty
    model = Model(id="qwen2.5:7b-instruct", provider="ollama")
    api = await resolve_api(model, auth)
    assert api.api_key is None


async def test_resolve_api_hosted_requires_credential() -> None:
    auth = InMemoryAuthStorage()
    model = Model(id="claude-sonnet-4-6", provider="anthropic")
    with pytest.raises(MissingCredential):
        await resolve_api(model, auth)
    await auth.set("anthropic", "sk-test")
    api = await resolve_api(model, auth)
    assert api.api_key == "sk-test"
    assert "anthropic.com" in api.base_url


async def test_env_auth_storage_reads_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    auth = EnvAuthStorage()
    assert await auth.get("openai") == "from-env"
    assert await auth.get("anthropic") is None
    listed = await auth.list_providers()
    assert "openai" in listed


# ─── session manager ─────────────────────────────────────────────


async def test_inmemory_session_manager_crud() -> None:
    sm = InMemorySessionManager()
    s = await sm.create(CreateAgentSessionOptions(agent_id="local", title="t1"))
    assert isinstance(s, AgentSession)
    fetched = await sm.get(s.id)
    assert fetched is s
    listed = await sm.list(agent_id="local")
    assert len(listed) == 1
    assert listed[0].title == "t1"
    assert await sm.delete(s.id) is True
    assert await sm.get(s.id) is None
