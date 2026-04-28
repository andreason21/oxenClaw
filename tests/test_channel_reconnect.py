"""Tests for the ChannelRouter reconnect watcher."""

from __future__ import annotations

import time

import pytest

from oxenclaw.channels.router import (
    ChannelRouter,
    is_auth_error,
)


def test_is_auth_error_detects_common_messages() -> None:
    assert is_auth_error("HTTP 401 unauthorized")
    assert is_auth_error("403 Forbidden")
    assert is_auth_error("token unauthorized")
    assert not is_auth_error("connection reset")
    assert not is_auth_error("")
    assert not is_auth_error(None)


def test_mark_failed_sets_backoff() -> None:
    router = ChannelRouter()
    state = router.mark_failed("bluebubbles", "main", error="timeout")
    assert state.attempts == 1
    assert state.next_retry > time.time()
    assert state.auth_error is False


def test_mark_failed_advances_backoff_ladder() -> None:
    router = ChannelRouter()
    s1 = router.mark_failed("bb", "main", error="boom")
    delay1 = s1.next_retry - time.time()
    s2 = router.mark_failed("bb", "main", error="boom")
    delay2 = s2.next_retry - time.time()
    assert delay2 > delay1


def test_mark_failed_auth_error_disables_retries() -> None:
    router = ChannelRouter()
    state = router.mark_failed("bb", "main", error="401 unauthorized")
    assert state.auth_error is True
    assert state.next_retry == 0.0


def test_mark_recovered_clears_failed_entry() -> None:
    router = ChannelRouter()
    router.mark_failed("bb", "main", error="x")
    router.mark_recovered("bb", "main")
    assert router.health()["failed"] == []


def test_health_reports_state() -> None:
    router = ChannelRouter()
    router.mark_failed("a", "1", error="boom")
    router.mark_failed("b", "1", error="401")
    h = router.health()
    assert h["bindings"] == 0
    cids = sorted(f["channel_id"] for f in h["failed"])
    assert cids == ["a", "b"]
    auth_flags = {f["channel_id"]: f["auth_error"] for f in h["failed"]}
    assert auth_flags == {"a": False, "b": True}


@pytest.mark.asyncio
async def test_start_stop_watcher_idempotent() -> None:
    router = ChannelRouter()
    await router.start_reconnect_watcher()
    await router.start_reconnect_watcher()  # no-op second call
    assert router._watcher_task is not None
    await router.stop_reconnect_watcher()
    assert router._watcher_task is None


@pytest.mark.asyncio
async def test_tick_recovers_when_probe_succeeds(monkeypatch) -> None:
    router = ChannelRouter()
    router.mark_failed("ch", "acct", error="boom")
    # Force the next_retry to "now" so the tick acts.
    state = router._failed_channels[("ch", "acct")]
    state.next_retry = time.time() - 1

    class _Result:
        ok = True
        error = ""

    async def _fake_probe(self, channel_id, account_id):
        return _Result()

    monkeypatch.setattr(ChannelRouter, "probe", _fake_probe)
    await router._tick_reconnect_watcher()
    assert router._failed_channels == {}


@pytest.mark.asyncio
async def test_tick_skips_auth_error() -> None:
    router = ChannelRouter()
    router.mark_failed("ch", "acct", error="401 unauthorized")
    # Even with next_retry = 0 the tick must not call probe.
    calls = []

    async def _spy_probe(self, channel_id, account_id):
        calls.append((channel_id, account_id))

        class R:
            ok = True
            error = ""

        return R()

    ChannelRouter.probe_orig = ChannelRouter.probe
    ChannelRouter.probe = _spy_probe  # type: ignore[assignment]
    try:
        await router._tick_reconnect_watcher()
    finally:
        ChannelRouter.probe = ChannelRouter.probe_orig  # type: ignore[assignment]
        del ChannelRouter.probe_orig
    assert calls == []
