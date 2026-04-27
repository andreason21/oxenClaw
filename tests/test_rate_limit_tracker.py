"""Rate-limit header tracker — parsing + tracker storage."""

from __future__ import annotations

import time

from oxenclaw.pi.rate_limit_tracker import (
    RateLimitState,
    RateLimitTracker,
    is_quota_exhausted,
    parse_rate_limit_headers,
)


def test_parse_returns_none_when_no_relevant_headers() -> None:
    state = parse_rate_limit_headers({"content-type": "application/json"})
    assert state is None


def test_parse_anthropic_headers() -> None:
    headers = {
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-requests-remaining": "950",
        "anthropic-ratelimit-requests-reset": "30",  # relative seconds
        "anthropic-ratelimit-tokens-limit": "100000",
        "anthropic-ratelimit-tokens-remaining": "85000",
        "anthropic-ratelimit-tokens-reset": "60",
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    assert state.requests_limit == 1000
    assert state.requests_remaining == 950
    assert state.tokens_limit == 100_000
    assert state.tokens_remaining == 85_000
    # Relative seconds → absolute epoch in the near future.
    assert state.requests_reset_at is not None
    assert state.requests_reset_at > time.time()


def test_parse_openai_headers_with_epoch_reset() -> None:
    """Reset values > 1e9 are treated as absolute epoch."""
    future_epoch = time.time() + 90
    headers = {
        "x-ratelimit-limit-requests": "60",
        "x-ratelimit-remaining-requests": "59",
        "x-ratelimit-reset-requests": str(int(future_epoch)),
        "x-ratelimit-limit-tokens": "150000",
        "x-ratelimit-remaining-tokens": "100",
        "x-ratelimit-reset-tokens": str(int(future_epoch)),
    }
    state = parse_rate_limit_headers(headers)
    assert state is not None
    # Within 5s of the original future_epoch.
    assert state.requests_reset_at is not None
    assert abs(state.requests_reset_at - future_epoch) < 5


def test_is_quota_exhausted_long_reset() -> None:
    state = RateLimitState(
        requests_remaining=0,
        requests_reset_at=time.time() + 600,  # 10 minutes
        observed_at=time.time(),
    )
    assert is_quota_exhausted(state) is True


def test_is_quota_exhausted_short_reset() -> None:
    """A 5s reset means we should retry, not rotate."""
    state = RateLimitState(
        requests_remaining=0,
        requests_reset_at=time.time() + 5,
        observed_at=time.time(),
    )
    assert is_quota_exhausted(state) is False


def test_is_quota_exhausted_remaining_above_zero() -> None:
    state = RateLimitState(
        requests_remaining=10,
        requests_reset_at=time.time() + 600,
        observed_at=time.time(),
    )
    assert is_quota_exhausted(state) is False


def test_tracker_record_and_peek() -> None:
    t = RateLimitTracker()
    state = RateLimitState(requests_remaining=5, observed_at=time.time())
    assert t.peek("anthropic", "key1") is None
    t.record("anthropic", "key1", state)
    assert t.peek("anthropic", "key1") is state
    # Different provider/key: separate entry.
    assert t.peek("anthropic", "key2") is None
    t.clear("anthropic")
    assert t.peek("anthropic", "key1") is None


def test_tracker_clear_all() -> None:
    t = RateLimitTracker()
    t.record("a", "k", RateLimitState(observed_at=time.time()))
    t.record("b", "k", RateLimitState(observed_at=time.time()))
    t.clear()
    assert t.peek("a", "k") is None
    assert t.peek("b", "k") is None
