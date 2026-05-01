"""Aggregate compaction timeout shield."""

from __future__ import annotations

import asyncio

import pytest

from oxenclaw.pi.run.compaction_timeout import with_compaction_timeout


async def test_passes_through_result_under_budget() -> None:
    async def fast() -> str:
        return "ok"

    out = await with_compaction_timeout(fast(), timeout_seconds=1.0)
    assert out == "ok"


async def test_disabled_when_timeout_none() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    out = await with_compaction_timeout(slow(), timeout_seconds=None)
    assert out == "ok"


async def test_disabled_when_timeout_zero_or_negative() -> None:
    async def fast() -> str:
        return "ok"

    out0 = await with_compaction_timeout(fast(), timeout_seconds=0)
    assert out0 == "ok"

    async def fast2() -> str:
        return "ok"

    out_neg = await with_compaction_timeout(fast2(), timeout_seconds=-5)
    assert out_neg == "ok"


async def test_returns_none_on_timeout_without_raising() -> None:
    async def hang() -> str:
        await asyncio.sleep(10.0)
        return "never"

    out = await with_compaction_timeout(hang(), timeout_seconds=0.05)
    assert out is None


async def test_invokes_on_timeout_callback() -> None:
    fired = {"count": 0}

    def cb() -> None:
        fired["count"] += 1

    async def hang() -> str:
        await asyncio.sleep(10.0)
        return "never"

    out = await with_compaction_timeout(hang(), timeout_seconds=0.05, on_timeout=cb)
    assert out is None
    assert fired["count"] == 1


async def test_swallows_callback_errors() -> None:
    """A buggy on_timeout must not break the run loop."""

    def bad() -> None:
        raise RuntimeError("logger blew up")

    async def hang() -> str:
        await asyncio.sleep(10.0)
        return "never"

    # Must not raise.
    out = await with_compaction_timeout(hang(), timeout_seconds=0.05, on_timeout=bad)
    assert out is None


async def test_propagates_inner_exception() -> None:
    """A raise inside the wrapped coro is NOT swallowed by the shield."""

    async def boom() -> str:
        raise ValueError("kapow")

    with pytest.raises(ValueError, match="kapow"):
        await with_compaction_timeout(boom(), timeout_seconds=1.0)
