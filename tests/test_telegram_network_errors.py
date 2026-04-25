"""Tests for the aiogram → SDK error taxonomy mapping."""

from __future__ import annotations

from sampyclaw.extensions.telegram.network_errors import classify, is_retryable
from sampyclaw.plugin_sdk.error_runtime import (
    ChannelError,
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)


def _make_method():  # type: ignore[no-untyped-def]
    from aiogram.methods import SendMessage

    return SendMessage(chat_id=1, text="x")


def test_classify_retry_after_is_rate_limited() -> None:
    from aiogram.exceptions import TelegramRetryAfter

    err = classify(
        TelegramRetryAfter(method=_make_method(), message="slow", retry_after=4)
    )
    assert isinstance(err, RateLimitedError)
    assert err.retry_after == 4.0


def test_classify_network_error() -> None:
    from aiogram.exceptions import TelegramNetworkError

    err = classify(TelegramNetworkError(method=_make_method(), message="boom"))
    assert isinstance(err, NetworkError)


def test_classify_bad_request_is_user_visible() -> None:
    from aiogram.exceptions import TelegramBadRequest

    err = classify(TelegramBadRequest(method=_make_method(), message="nope"))
    assert isinstance(err, UserVisibleError)


def test_classify_forbidden_is_user_visible() -> None:
    from aiogram.exceptions import TelegramForbiddenError

    err = classify(TelegramForbiddenError(method=_make_method(), message="blocked"))
    assert isinstance(err, UserVisibleError)


def test_classify_unknown_falls_back_to_channel_error() -> None:
    err = classify(RuntimeError("weird"))
    assert type(err) is ChannelError


def test_is_retryable_network_and_retry_after() -> None:
    from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

    assert is_retryable(TelegramNetworkError(method=_make_method(), message="x"))
    assert is_retryable(
        TelegramRetryAfter(method=_make_method(), message="x", retry_after=1)
    )


def test_is_not_retryable_bad_request_or_forbidden() -> None:
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

    assert not is_retryable(TelegramBadRequest(method=_make_method(), message="x"))
    assert not is_retryable(TelegramForbiddenError(method=_make_method(), message="x"))


def test_is_retryable_default_true_for_unknown() -> None:
    assert is_retryable(RuntimeError("???"))
