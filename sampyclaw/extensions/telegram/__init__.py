"""Telegram channel extension. Port of openclaw extensions/telegram/*."""

from sampyclaw.extensions.telegram.accounts import TelegramAccountRegistry
from sampyclaw.extensions.telegram.bot_core import (
    TELEGRAM_CHANNEL_ID,
    UpdateDeduplicator,
    create_bot,
    envelope_from_message,
)
from sampyclaw.extensions.telegram.channel import TelegramChannel
from sampyclaw.extensions.telegram.monitor import TelegramPollingSession
from sampyclaw.extensions.telegram.network_errors import classify, is_retryable
from sampyclaw.extensions.telegram.polling_runner import PollingRunner
from sampyclaw.extensions.telegram.send import send_message_telegram
from sampyclaw.extensions.telegram.thread_bindings import ThreadBinding, ThreadBindings
from sampyclaw.extensions.telegram.token import TokenResolver

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
