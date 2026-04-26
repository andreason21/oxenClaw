"""Telegram channel extension. Port of openclaw extensions/telegram/*."""

from oxenclaw.extensions.telegram.accounts import TelegramAccountRegistry
from oxenclaw.extensions.telegram.bot_core import (
    TELEGRAM_CHANNEL_ID,
    UpdateDeduplicator,
    create_bot,
    envelope_from_message,
)
from oxenclaw.extensions.telegram.channel import TelegramChannel
from oxenclaw.extensions.telegram.monitor import TelegramPollingSession
from oxenclaw.extensions.telegram.network_errors import classify, is_retryable
from oxenclaw.extensions.telegram.polling_runner import PollingRunner
from oxenclaw.extensions.telegram.send import send_message_telegram
from oxenclaw.extensions.telegram.thread_bindings import ThreadBinding, ThreadBindings
from oxenclaw.extensions.telegram.token import TokenResolver

__all__ = [
    "TELEGRAM_CHANNEL_ID",
    "PollingRunner",
    "TelegramAccountRegistry",
    "TelegramChannel",
    "TelegramPollingSession",
    "ThreadBinding",
    "ThreadBindings",
    "TokenResolver",
    "UpdateDeduplicator",
    "classify",
    "create_bot",
    "envelope_from_message",
    "is_retryable",
    "send_message_telegram",
]
