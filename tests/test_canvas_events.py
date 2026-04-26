"""Unit tests for oxenclaw.canvas.events.CanvasEventBus."""

from __future__ import annotations

import asyncio

import pytest

from oxenclaw.canvas.events import CanvasEvent, CanvasEventBus


@pytest.mark.asyncio
async def test_publish_to_no_subscribers_returns_zero() -> None:
    bus = CanvasEventBus()
    delivered = bus.publish(CanvasEvent(kind="present", agent_id="a"))
    assert delivered == 0


@pytest.mark.asyncio
async def test_subscribe_receives_published_events() -> None:
    bus = CanvasEventBus()
    q = await bus.subscribe()
    bus.publish(CanvasEvent(kind="present", agent_id="a", payload={"x": 1}))
    evt = await asyncio.wait_for(q.get(), timeout=1.0)
    assert evt.kind == "present"
    assert evt.payload == {"x": 1}
    await bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_full_subscriber_drops_event_does_not_block() -> None:
    bus = CanvasEventBus(queue_size=1)
    q = await bus.subscribe()
    bus.publish(CanvasEvent(kind="present", agent_id="a"))
    delivered2 = bus.publish(CanvasEvent(kind="hide", agent_id="a"))
    assert delivered2 == 0  # second event dropped, publisher not blocked
    first = await asyncio.wait_for(q.get(), timeout=1.0)
    assert first.kind == "present"
    await bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_eval_request_resolution_round_trip() -> None:
    bus = CanvasEventBus()
    rid = bus.new_eval_request_id()
    fut = bus.register_eval_waiter(rid)
    assert bus.resolve_eval(rid, {"value": 42}) is True
    assert (await fut) == {"value": 42}


@pytest.mark.asyncio
async def test_eval_request_rejection() -> None:
    bus = CanvasEventBus()
    rid = bus.new_eval_request_id()
    fut = bus.register_eval_waiter(rid)
    assert bus.reject_eval(rid, RuntimeError("boom")) is True
    with pytest.raises(RuntimeError):
        await fut


@pytest.mark.asyncio
async def test_resolve_unknown_returns_false() -> None:
    bus = CanvasEventBus()
    assert bus.resolve_eval("missing", "x") is False


@pytest.mark.asyncio
async def test_stream_iterates_until_break() -> None:
    bus = CanvasEventBus()
    received = []

    async def consumer() -> None:
        async for evt in bus.stream():
            received.append(evt.kind)
            if len(received) == 2:
                break

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    bus.publish(CanvasEvent(kind="present", agent_id="a"))
    bus.publish(CanvasEvent(kind="hide", agent_id="a"))
    await asyncio.wait_for(task, timeout=2.0)
    assert received == ["present", "hide"]
