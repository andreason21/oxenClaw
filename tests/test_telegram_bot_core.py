"""Tests for Telegram bot_core: update dedup and Message→InboundEnvelope translation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sampyclaw.extensions.telegram.bot_core import (
    UpdateDeduplicator,
    envelope_from_message,
)


def _make_message(
    *,
    text: str | None = "hello",
    caption: str | None = None,
    message_id: int = 1,
    chat_id: int = 42,
    user_id: int | None = 100,
    thread_id: int | None = None,
):  # type: ignore[no-untyped-def]
    from aiogram.types import Chat, Message, User

    user = None
    if user_id is not None:
        user = User(id=user_id, is_bot=False, first_name="Alice", last_name="Wonder")
    return Message.model_validate(
        {
            "message_id": message_id,
            "date": int(datetime(2026, 4, 24, tzinfo=UTC).timestamp()),
            "chat": Chat(id=chat_id, type="private").model_dump(),
            "from": user.model_dump() if user else None,
            "text": text,
            "caption": caption,
            "message_thread_id": thread_id,
        }
    )


def test_dedup_first_seen_is_false_second_true() -> None:
    d = UpdateDeduplicator()
    assert d.seen(1) is False
    assert d.seen(1) is True
    assert d.seen(2) is False


def test_dedup_evicts_oldest_past_capacity() -> None:
    d = UpdateDeduplicator(capacity=3)
    for i in range(3):
        d.seen(i)
    assert len(d) == 3
    d.seen(99)  # triggers eviction of 0
    assert len(d) == 3
    assert d.seen(0) is False  # was evicted, so now unseen again
    assert d.seen(99) is True


def test_dedup_rejects_non_positive_capacity() -> None:
    with pytest.raises(ValueError):
        UpdateDeduplicator(capacity=0)


def test_envelope_from_text_message() -> None:
    env = envelope_from_message(_make_message(text="hi there"), account_id="main")
    assert env is not None
    assert env.channel == "telegram"
    assert env.account_id == "main"
    assert env.text == "hi there"
    assert env.target.chat_id == "42"
    assert env.target.thread_id is None
    assert env.sender_id == "100"
    assert env.sender_display_name == "Alice Wonder"


def test_envelope_from_caption_only() -> None:
    env = envelope_from_message(
        _make_message(text=None, caption="photo caption"), account_id="main"
    )
    assert env is not None
    assert env.text == "photo caption"


def test_envelope_without_text_returns_none() -> None:
    env = envelope_from_message(
        _make_message(text=None, caption=None), account_id="main"
    )
    assert env is None


def test_envelope_preserves_thread_id_as_string() -> None:
    env = envelope_from_message(_make_message(thread_id=777), account_id="main")
    assert env is not None
    assert env.target.thread_id == "777"


def test_envelope_anonymous_when_no_sender() -> None:
    env = envelope_from_message(_make_message(user_id=None), account_id="main")
    assert env is not None
    assert env.sender_id == "anonymous"
    assert env.sender_display_name is None
