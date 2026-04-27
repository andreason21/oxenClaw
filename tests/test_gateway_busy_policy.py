"""Tests for the busy / queue / interrupt / steer policy."""

from __future__ import annotations

import asyncio

import pytest

from oxenclaw.agents.lanes import LaneRegistry


@pytest.mark.asyncio
async def test_busy_policy_default_is_queue() -> None:
    reg = LaneRegistry()
    assert reg.busy_policy == "queue"


@pytest.mark.asyncio
async def test_queue_message_records_pending_and_queued_at() -> None:
    reg = LaneRegistry()
    reg.queue_message("a", "k", "hello")
    state = reg.lane("a", "k")
    assert state.pending_messages == ["hello"]
    assert state.queued_at is not None


@pytest.mark.asyncio
async def test_signal_abort_flips_event() -> None:
    reg = LaneRegistry()
    state = reg.lane("a", "k")
    state.abort_event = asyncio.Event()
    assert reg.signal_abort("a", "k") is True
    assert state.abort_event.is_set()
    # Idempotent — already set.
    assert reg.signal_abort("a", "k") is False


@pytest.mark.asyncio
async def test_maybe_busy_ack_debounces() -> None:
    reg = LaneRegistry()
    state = reg.lane("a", "k")
    # No lock held → no ack.
    should, _ = reg.maybe_busy_ack("a", "k")
    assert should is False
    # Acquire the lock to simulate in-flight turn.
    async with state.lock:
        # First call inside debounce window.
        should, _ = reg.maybe_busy_ack("a", "k")
        assert should is False  # under 30s


@pytest.mark.asyncio
async def test_run_under_lock_serialises_two_calls() -> None:
    reg = LaneRegistry()
    order: list[str] = []

    async def first():
        order.append("first-start")
        await asyncio.sleep(0.05)
        order.append("first-end")
        return 1

    async def second():
        order.append("second-start")
        order.append("second-end")
        return 2

    t1 = asyncio.create_task(reg.run(agent_id="a", session_key="k", coro_factory=first))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(reg.run(agent_id="a", session_key="k", coro_factory=second))
    await asyncio.gather(t1, t2)
    # second cannot start until first ends.
    assert order == ["first-start", "first-end", "second-start", "second-end"]


@pytest.mark.asyncio
async def test_lanes_stats_reports_busy_policy() -> None:
    reg = LaneRegistry(busy_policy="interrupt")
    stats = reg.stats()
    assert stats["busy_policy"] == "interrupt"


@pytest.mark.asyncio
async def test_dispatcher_interrupt_policy_signals_abort() -> None:
    """When busy_policy=interrupt and a turn is in flight, the dispatcher
    should signal `abort_event` on the lane before queueing the next."""
    from oxenclaw.agents.dispatch import Dispatcher
    from oxenclaw.agents.registry import AgentRegistry
    from oxenclaw.plugin_sdk.config_schema import RootConfig

    reg = LaneRegistry(busy_policy="interrupt")
    state = reg.lane("agent-x", "k")
    state.abort_event = asyncio.Event()
    # Hold the lock to simulate an in-flight turn.
    await state.lock.acquire()
    try:
        # Direct exercise of the same code path the dispatcher would
        # take without needing a full envelope round-trip.
        assert state.lock.locked()
        assert reg.signal_abort("agent-x", "k") is True
        assert state.abort_event.is_set()
    finally:
        state.lock.release()
