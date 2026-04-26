"""Tests for ChannelRunner: generic restart-on-error around `channel.monitor()`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from sampyclaw.channels.runner import ChannelRunner
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, MonitorOpts


class _FakeChannel:
    """Channel stand-in whose monitor() follows a scripted behaviour list."""

    def __init__(self, behaviours: list, *, channel_id: str = "fake") -> None:  # type: ignore[type-arg]
        self.id = channel_id
        self._behaviours = list(behaviours)
        self.monitor_calls = 0

    async def monitor(self, opts: MonitorOpts) -> None:
        self.monitor_calls += 1
        if not self._behaviours:
            return
        beh = self._behaviours.pop(0)
        if isinstance(beh, BaseException):
            raise beh
        # awaitable or literal None
        if isinstance(beh, Callable):  # type: ignore[arg-type]
            await beh()

    async def send(self, *a, **k):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def probe(self, *a, **k):  # type: ignore[no-untyped-def]
        raise NotImplementedError


async def _noop(_: InboundEnvelope) -> None:
    return None


def _make(channel: _FakeChannel, **kwargs):  # type: ignore[no-untyped-def]
    sleeps: list[float] = []

    async def fake_sleep(secs: float) -> None:
        sleeps.append(secs)
        await asyncio.sleep(0)  # yield to let concurrent tasks run

    runner = ChannelRunner(
        channel,
        MonitorOpts(account_id="main", on_inbound=_noop),
        jitter=0.0,
        sleep=fake_sleep,
        **kwargs,
    )
    return runner, sleeps


async def test_restart_after_error_then_stop() -> None:
    ch = _FakeChannel([RuntimeError("boom"), None, None])
    runner, _ = _make(ch)

    async def _stop_after_second_monitor() -> None:
        while ch.monitor_calls < 2:
            await asyncio.sleep(0)
        await runner.stop()

    await asyncio.gather(runner.run_forever(), _stop_after_second_monitor())
    assert ch.monitor_calls >= 2
    assert runner.restart_count >= 1


async def test_backoff_doubles_then_caps() -> None:
    ch = _FakeChannel([RuntimeError("e")] * 5)
    runner, sleeps = _make(ch, initial_backoff=1.0, max_backoff=4.0)

    async def _stop() -> None:
        while ch.monitor_calls < 5:
            await asyncio.sleep(0)
        await runner.stop()

    await asyncio.gather(runner.run_forever(), _stop())
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert all(s <= 4.0 for s in sleeps)


async def test_clean_return_also_triggers_restart() -> None:
    ch = _FakeChannel([None, None])
    runner, _ = _make(ch)

    async def _stop() -> None:
        while ch.monitor_calls < 2:
            await asyncio.sleep(0)
        await runner.stop()

    await asyncio.gather(runner.run_forever(), _stop())
    assert ch.monitor_calls >= 2


async def test_cancellation_propagates() -> None:
    async def _hang() -> None:
        await asyncio.sleep(10)

    ch = _FakeChannel([_hang])
    runner, _ = _make(ch)

    task = asyncio.create_task(runner.run_forever())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_exposes_ids() -> None:
    ch = _FakeChannel([], channel_id="discord")
    runner, _ = _make(ch)
    assert runner.channel_id == "discord"
    assert runner.account_id == "main"


def test_rejects_bad_config() -> None:
    ch = _FakeChannel([])
    opts = MonitorOpts(account_id="main", on_inbound=_noop)
    with pytest.raises(ValueError):
        ChannelRunner(ch, opts, initial_backoff=0)
    with pytest.raises(ValueError):
        ChannelRunner(ch, opts, initial_backoff=5, max_backoff=1)
    with pytest.raises(ValueError):
        ChannelRunner(ch, opts, jitter=1.5)
    with pytest.raises(ValueError):
        ChannelRunner(ch, opts, max_restarts=-1)


async def test_max_restarts_gives_up() -> None:
    """Permanently broken channel must stop after max_restarts."""
    ch = _FakeChannel([RuntimeError("nope")] * 10)
    runner, _ = _make(ch, max_restarts=3)
    await runner.run_forever()  # returns once cap hit, no external stop needed
    assert runner.gave_up is True
    assert runner.restart_count == 3
    # 1 initial + 3 restart attempts = 4 monitor calls.
    assert ch.monitor_calls == 4


async def test_backoff_resets_after_long_stable_run() -> None:
    """A monitor that ran stably before failing should restart fast, not slow."""
    times = iter([0.0, 0.5, 0.6, 200.0, 200.1, 200.2, 300.0, 300.1])

    def _clock() -> float:
        return next(times)

    async def _quick() -> None:
        return None

    ch = _FakeChannel([RuntimeError("e1"), _quick, RuntimeError("e2")])
    runner, sleeps = _make(
        ch,
        initial_backoff=1.0,
        max_backoff=8.0,
        stable_reset_seconds=60.0,
        clock=_clock,
    )

    async def _stop() -> None:
        while ch.monitor_calls < 3:
            await asyncio.sleep(0)
        await runner.stop()

    await asyncio.gather(runner.run_forever(), _stop())
    # iter1: monitor raises after 0.5s (no reset) → sleep=1.0 → backoff=2.0
    # iter2: monitor returned cleanly after 199.4s (RESET) → sleep=1.0
    assert sleeps[0] == 1.0
    assert sleeps[1] == 1.0  # reset, not 2.0
