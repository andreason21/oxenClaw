"""End-to-end tests: a photo on InboundEnvelope → agent receives image
content blocks in the right shape for each provider.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.local_agent import LocalAgent
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.plugin_sdk.channel_contract import (
    ChannelTarget,
    InboundEnvelope,
    MediaItem,
)


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _ctx() -> AgentContext:
    return AgentContext(agent_id="test", session_key="s")


def _envelope(*, text: str | None = None, photo_b64: str | None = None) -> InboundEnvelope:
    media: list[MediaItem] = []
    if photo_b64:
        media.append(
            MediaItem(
                kind="photo",
                source=f"data:image/jpeg;base64,{photo_b64}",
                mime_type="image/jpeg",
            )
        )
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="100",
        text=text,
        media=media,
        received_at=0.0,
    )


def _jpeg_b64() -> str:
    raw = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"x" * 64
    return base64.b64encode(raw).decode()


# ─── LocalAgent ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_agent_with_image_capable_model_sends_image_url_block(
    tmp_path,
    monkeypatch,
):  # type: ignore[no-untyped-def]
    """When model supports images, LocalAgent puts the photo into the
    request payload as an OpenAI-shape image_url block."""
    captured: list[dict] = []

    async def fake_chat_completion(self, *, messages, tools):  # type: ignore[no-untyped-def]
        captured.append({"messages": messages, "tools": tools})
        # Simulate a tool-free assistant reply that ends the loop.
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "I see a JPEG image.",
                        "tool_calls": [],
                    },
                }
            ]
        }

    monkeypatch.setattr(LocalAgent, "_chat_complete", fake_chat_completion, raising=True)

    async def _noop(self):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(LocalAgent, "_maybe_warmup", _noop, raising=True)

    agent = LocalAgent(
        model="gemma4:latest",  # cataloged as supports_image_input=True
        paths=_paths(tmp_path),
        warmup=False,
        stream=False,
    )

    env = _envelope(text="what's in this?", photo_b64=_jpeg_b64())
    [_ async for _ in agent.handle(env, _ctx())]

    # First call carries the user message at the end of `messages`.
    assert captured, "agent did not call the model"
    user_msg = captured[0]["messages"][-1]
    assert user_msg["role"] == "user"
    blocks = user_msg["content"]
    assert isinstance(blocks, list)
    types = [b["type"] for b in blocks]
    assert "image_url" in types
    assert "text" in types
    img_block = next(b for b in blocks if b["type"] == "image_url")
    assert img_block["image_url"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_local_agent_with_text_only_model_drops_image_with_note(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    captured: list[dict] = []

    async def fake_chat_completion(self, *, messages, tools):  # type: ignore[no-untyped-def]
        captured.append({"messages": messages, "tools": tools})
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "tool_calls": [],
                    },
                }
            ]
        }

    monkeypatch.setattr(LocalAgent, "_chat_complete", fake_chat_completion, raising=True)

    async def _noop(self):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(LocalAgent, "_maybe_warmup", _noop, raising=True)

    agent = LocalAgent(
        # No image support in catalog — gemma3:4b is supports_image_input=False.
        model="gemma3:4b",
        paths=_paths(tmp_path),
        warmup=False,
        stream=False,
    )
    env = _envelope(text="see this", photo_b64=_jpeg_b64())
    [_ async for _ in agent.handle(env, _ctx())]

    user_msg = captured[0]["messages"][-1]
    assert user_msg["role"] == "user"
    # Plain string content — no image_url blocks anywhere.
    assert isinstance(user_msg["content"], str)
    assert "see this" in user_msg["content"]
    assert "image(s) dropped" in user_msg["content"]
    assert "gemma3:4b" in user_msg["content"]


@pytest.mark.asyncio
async def test_local_agent_image_only_no_text_still_dispatches(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """A photo without caption should still trigger a turn."""
    captured: list[dict] = []

    async def fake_chat_completion(self, *, messages, tools):  # type: ignore[no-untyped-def]
        captured.append({"messages": messages, "tools": tools})
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "saw it",
                        "tool_calls": [],
                    },
                }
            ]
        }

    monkeypatch.setattr(LocalAgent, "_chat_complete", fake_chat_completion, raising=True)

    async def _noop(self):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(LocalAgent, "_maybe_warmup", _noop, raising=True)

    agent = LocalAgent(
        model="gemma4:latest",
        paths=_paths(tmp_path),
        warmup=False,
        stream=False,
    )
    env = _envelope(text=None, photo_b64=_jpeg_b64())
    outs = [sp async for sp in agent.handle(env, _ctx())]

    assert captured, "agent should have been invoked even without text"
    assert outs, "agent should have produced at least one reply chunk"
    user_msg = captured[0]["messages"][-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert any(b["type"] == "image_url" for b in user_msg["content"])


# ─── PiAgent ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pi_agent_with_image_capable_model_appends_image_block(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """PiAgent UserMessage should carry a list[ImageContent, TextContent]
    when the model supports images. Drives the path without going to
    the network — we just inspect `session.messages` after handle()."""
    from oxenclaw.agents.factory import build_agent
    from oxenclaw.pi.messages import ImageContent, TextContent, UserMessage

    agent = build_agent(agent_id="pi-test", provider="pi")  # default = gemma4:latest
    env = _envelope(text="caption", photo_b64=_jpeg_b64())

    async def _stub_run_turn(*args, **kwargs):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        return SimpleNamespace(
            final_message=type("M", (), {"content": [TextContent(text="ok")]})(),
            appended_messages=[],
            new_messages=[],
            iterations=1,
            usage_total={},
        )

    # Monkey-patch via pytest fixture so the binding reverts after the test.
    import oxenclaw.agents.pi_agent as pi_module

    monkeypatch.setattr(pi_module, "run_agent_turn", _stub_run_turn)

    [_ async for _ in agent.handle(env, _ctx())]

    # Locate the session and verify the last user message shape.
    session = next(iter(agent._sessions._sessions.values()))  # type: ignore[attr-defined]
    user_msgs = [m for m in session.messages if isinstance(m, UserMessage)]
    assert user_msgs, "no user message was appended"
    last = user_msgs[-1]
    assert isinstance(last.content, list)
    has_image = any(isinstance(b, ImageContent) for b in last.content)
    has_text = any(isinstance(b, TextContent) for b in last.content)
    assert has_image and has_text


# Anthropic image-block coverage now lives in `tests/test_pi_providers.py`
# (Anthropic provider serializer test) — the standalone AnthropicAgent
# was removed in favour of routing `--provider anthropic` through PiAgent.
