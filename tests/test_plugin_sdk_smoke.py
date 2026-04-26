"""Smoke tests that the plugin SDK imports cleanly and its contracts are well-formed."""

from __future__ import annotations

import pytest

from oxenclaw.plugin_sdk import (
    ChannelPlugin,
    InboundEnvelope,
    RateLimitedError,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget
from oxenclaw.plugin_sdk.reply_runtime import chunk_text


def test_send_params_roundtrip() -> None:
    params = SendParams(
        target=ChannelTarget(channel="telegram", account_id="acct", chat_id="123"),
        text="hello",
    )
    dumped = params.model_dump()
    again = SendParams.model_validate(dumped)
    assert again.text == "hello"
    assert again.target.chat_id == "123"


def test_inbound_envelope_minimal() -> None:
    env = InboundEnvelope(
        channel="telegram",
        account_id="acct",
        target=ChannelTarget(channel="telegram", account_id="acct", chat_id="123"),
        sender_id="user-1",
        received_at=0.0,
    )
    assert env.text is None
    assert env.media == []


def test_rate_limited_error_carries_retry_after() -> None:
    err = RateLimitedError("slow down", retry_after=1.5)
    assert err.retry_after == 1.5


def test_chunk_text_respects_limit() -> None:
    text = "a" * 1000
    chunks = list(chunk_text(text, 100))
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_text_prefers_newline_boundaries() -> None:
    text = "first paragraph.\n\nsecond paragraph which is longer than the limit boundary."
    chunks = list(chunk_text(text, 30))
    assert chunks[0].startswith("first paragraph")


def test_chunk_text_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError):
        list(chunk_text("abc", 0))


def test_channel_plugin_protocol_runtime_check() -> None:
    class StubPlugin:
        id = "stub"

        async def send(self, params: SendParams) -> SendResult:
            return SendResult(message_id="1", timestamp=0.0)

        async def monitor(self, opts: object) -> None:  # pragma: no cover
            return None

        async def probe(self, opts: object) -> object:  # pragma: no cover
            raise NotImplementedError

    assert isinstance(StubPlugin(), ChannelPlugin)
