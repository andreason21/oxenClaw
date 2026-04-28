"""LaneRegistry: per-session serialisation + optional global cap."""

from __future__ import annotations

import asyncio

from oxenclaw.agents.lanes import LaneRegistry


async def test_session_lane_serialises_same_session_calls() -> None:
    """Two concurrent calls on the same (agent, session) MUST run in
    sequence — no overlap."""
    lanes = LaneRegistry()
    counter = {"in_flight": 0, "max": 0}

    async def task() -> int:
        counter["in_flight"] += 1
        counter["max"] = max(counter["max"], counter["in_flight"])
        await asyncio.sleep(0.01)
        counter["in_flight"] -= 1
        return 42

    a = lanes.run(agent_id="a", session_key="s1", coro_factory=task)
    b = lanes.run(agent_id="a", session_key="s1", coro_factory=task)
    await asyncio.gather(a, b)
    assert counter["max"] == 1


async def test_session_lanes_independent_run_in_parallel() -> None:
    """Different sessions don't block each other."""
    lanes = LaneRegistry()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def first() -> None:
        started.set()
        await finished.wait()

    async def second() -> str:
        await started.wait()
        # While `first` is still in-flight in s1, second should run in s2.
        finished.set()
        return "ok"

    a = asyncio.create_task(lanes.run(agent_id="a", session_key="s1", coro_factory=first))
    b = asyncio.create_task(lanes.run(agent_id="a", session_key="s2", coro_factory=second))
    result = await asyncio.gather(a, b)
    assert result[1] == "ok"


async def test_global_cap_limits_concurrent_runs() -> None:
    lanes = LaneRegistry(global_concurrency=2)
    counter = {"in_flight": 0, "max": 0}

    async def slow() -> None:
        counter["in_flight"] += 1
        counter["max"] = max(counter["max"], counter["in_flight"])
        await asyncio.sleep(0.02)
        counter["in_flight"] -= 1

    tasks = [lanes.run(agent_id="a", session_key=f"s{i}", coro_factory=slow) for i in range(5)]
    await asyncio.gather(*tasks)
    assert counter["max"] == 2


def test_stats_shape() -> None:
    lanes = LaneRegistry(global_concurrency=4)
    s = lanes.stats()
    assert s["global_concurrency"] == 4
    assert "session_lock_count" in s
    assert "session_locks_held" in s
