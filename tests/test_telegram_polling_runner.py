"""Tests for PollingRunner: restart-on-error backoff + stop."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sampyclaw.extensions.telegram.polling_runner import PollingRunner


class _FakeSession:
    def __init__(self, behaviors: list) -> None:  # type: ignore[type-arg]
        self.behaviors = list(behaviors)
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        if not self.behaviors:
            # runner not explicitly stopped — simulate clean return
            return
        beh = self.behaviors.pop(0)
        if isinstance(beh, BaseException):
            raise beh

    async def stop(self) -> None:
        self.stop_calls += 1


def _runner(session, **kwargs):  # type: ignore[no-untyped-def]
    # bypass asyncio.sleep to make tests fast and deterministic, but yield to
    # the event loop so concurrent stop tasks get a turn.
    import asyncio as _asyncio

    sleep_calls = []

    async def fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)
        await _asyncio.sleep(0)

    runner = PollingRunner(session, sleep=fake_sleep, jitter=0.0, **kwargs)
    return runner, sleep_calls


async def test_runner_restarts_after_retryable_error() -> None:
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    network_err = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="down")

    session = _FakeSession([network_err, None])  # first raises, second returns clean

    runner, sleeps = _runner(session)
    runner._stopped = False

    # Stop after the second start so run_forever exits.
    async def _stop_after_second_start() -> None:
        # spin until start_calls >= 2, then stop
        import asyncio

        while session.start_calls < 2:
            await asyncio.sleep(0)
        await runner.stop()

    import asyncio

    await asyncio.gather(runner.run_forever(), _stop_after_second_start())
    assert session.start_calls == 2
    assert runner.restart_count >= 1
    assert len(sleeps) >= 1


async def test_runner_backoff_doubles_up_to_max() -> None:
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage

    session = _FakeSession(
        [TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="x")] * 5
    )
    runner, sleeps = _runner(session, initial_backoff=1.0, max_backoff=4.0)

    async def _stop() -> None:
        import asyncio

        while session.start_calls < 5:
            await asyncio.sleep(0)
        await runner.stop()

    import asyncio

    await asyncio.gather(runner.run_forever(), _stop())
    # Sleeps grow 1, 2, 4, 4, 4 (capped)
    assert sleeps[:3] == [1.0, 2.0, 4.0]
    assert all(s <= 4.0 for s in sleeps)


async def test_runner_propagates_non_retryable_error() -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    bad = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="nope")
    session = _FakeSession([bad])
    runner, _ = _runner(session)
    with pytest.raises(TelegramBadRequest):
        await runner.run_forever()


async def test_runner_rejects_bad_backoff_config() -> None:
    session = _FakeSession([])
    with pytest.raises(ValueError):
        PollingRunner(session, initial_backoff=0)
    with pytest.raises(ValueError):
        PollingRunner(session, initial_backoff=5, max_backoff=1)
    with pytest.raises(ValueError):
        PollingRunner(session, jitter=1.5)


async def test_stop_sets_flag_and_stops_session() -> None:
    session = _FakeSession([])
    session.stop = AsyncMock()  # type: ignore[assignment]
    runner, _ = _runner(session)
    await runner.stop()
    assert runner._stopped is True
    session.stop.assert_awaited_once()
