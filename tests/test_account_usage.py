"""Tests for account usage normalisation."""

from __future__ import annotations

from typing import Any

import pytest

from oxenclaw.pi import account_usage


@pytest.fixture(autouse=True)
def _reset_request_fn():
    yield
    account_usage.reset_request_fn()


def _make_canned(responses: dict[str, dict[str, Any] | None]):
    async def _fn(method: str, url: str, headers, body):
        return responses.get(url)
    return _fn


@pytest.mark.asyncio
async def test_anthropic_usage_snapshot_normalises_windows() -> None:
    payload = {
        "five_hour": {
            "utilization": 0.42,
            "resets_at": "2026-01-01T00:00:00Z",
        },
        "seven_day": {
            "utilization": 12.5,  # already a percent
        },
        "extra_usage": {
            "is_enabled": True,
            "used_credits": 5.0,
            "monthly_limit": 25.0,
        },
    }
    account_usage.set_request_fn(
        _make_canned({"https://api.anthropic.com/api/oauth/usage": payload})
    )
    snap = await account_usage.fetch_anthropic_usage("oauth-token")
    assert snap is not None
    assert snap.provider == "anthropic"
    labels = [w.label for w in snap.windows]
    assert "Current session" in labels
    assert "Current week" in labels
    five_hour = next(w for w in snap.windows if w.label == "Current session")
    assert 41.9 < five_hour.used_percent < 42.1
    assert five_hour.reset_at is not None  # ISO parsed to epoch
    assert snap.extra_credit == 20.0


@pytest.mark.asyncio
async def test_anthropic_usage_returns_none_on_404() -> None:
    async def _fn(method, url, headers, body):
        return None  # default httpx layer maps 404 → None

    account_usage.set_request_fn(_fn)
    snap = await account_usage.fetch_anthropic_usage("plain-key")
    assert snap is None


@pytest.mark.asyncio
async def test_openrouter_usage_combines_credits_and_key() -> None:
    responses = {
        "https://openrouter.ai/api/v1/credits": {
            "data": {"total_credits": 10.0, "total_usage": 3.5}
        },
        "https://openrouter.ai/api/v1/key": {
            "data": {"limit": 20.0, "limit_remaining": 12.0}
        },
    }
    account_usage.set_request_fn(_make_canned(responses))
    snap = await account_usage.fetch_openrouter_usage("sk-or-test")
    assert snap is not None
    assert snap.provider == "openrouter"
    assert snap.extra_credit == 6.5
    assert len(snap.windows) == 1
    w = snap.windows[0]
    assert w.label == "API key quota"
    assert 39.9 < w.used_percent < 40.1


@pytest.mark.asyncio
async def test_openrouter_usage_none_when_both_endpoints_404() -> None:
    async def _fn(method, url, headers, body):
        return None

    account_usage.set_request_fn(_fn)
    snap = await account_usage.fetch_openrouter_usage("sk-or-x")
    assert snap is None


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_provider() -> None:
    async def _fn(method, url, headers, body):
        if "anthropic" in url:
            return {"five_hour": {"utilization": 0.5}}
        return None

    account_usage.set_request_fn(_fn)
    snap = await account_usage.fetch_account_usage("anthropic", "sk-ant-x")
    assert snap is not None
    assert snap.provider == "anthropic"
    snap = await account_usage.fetch_account_usage("openai", "sk-x")
    assert snap is None  # not implemented


@pytest.mark.asyncio
async def test_to_dict_round_trip() -> None:
    snap = account_usage.AccountUsageSnapshot(
        provider="anthropic",
        windows=[
            account_usage.AccountUsageWindow(label="Session", used_percent=42.0)
        ],
        extra_credit=5.0,
    )
    d = snap.to_dict()
    assert d["provider"] == "anthropic"
    assert d["windows"][0]["used_percent"] == 42.0
    assert d["extra_credit"] == 5.0
