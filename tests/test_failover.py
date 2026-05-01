"""Assistant-failover decision logic."""

from __future__ import annotations

from oxenclaw.pi.messages import AssistantMessage, TextContent
from oxenclaw.pi.run.attempt import AttemptResult
from oxenclaw.pi.run.failover import (
    FailoverDecision,
    resolve_next_model,
    should_failover,
)
from oxenclaw.pi.streaming import ErrorEvent


def _result(*, error: ErrorEvent | None = None, stop_reason: str = "end_turn") -> AttemptResult:
    msg = AssistantMessage(content=[TextContent(text="")], stop_reason=stop_reason)
    return AttemptResult(message=msg, error=error)


def test_no_failover_at_end_of_chain() -> None:
    decision = should_failover(
        result=_result(),
        chain=["a"],
        chain_cursor=0,
        empty_streak=0,
    )
    assert decision == FailoverDecision(failover=False, reason="end_of_chain")


def test_failover_on_provider_error() -> None:
    err = ErrorEvent(message="overloaded", retryable=False)
    decision = should_failover(
        result=_result(error=err),
        chain=["a", "b"],
        chain_cursor=0,
        empty_streak=0,
    )
    assert decision.failover
    assert decision.next_model_id == "b"
    assert "non_retryable" in decision.reason


def test_failover_on_empty_streak_threshold() -> None:
    decision = should_failover(
        result=_result(),
        chain=["a", "b"],
        chain_cursor=0,
        empty_streak=3,
        empty_streak_threshold=3,
    )
    assert decision.failover
    assert decision.reason.startswith("empty_streak")


def test_failover_on_special_stop_reason() -> None:
    decision = should_failover(
        result=_result(stop_reason="overloaded"),
        chain=["a", "b"],
        chain_cursor=0,
        empty_streak=0,
    )
    assert decision.failover
    assert "overloaded" in decision.reason


def test_failover_custom_predicate() -> None:
    """Custom predicate fires regardless of other heuristics."""
    decision = should_failover(
        result=_result(stop_reason="end_turn"),
        chain=["a", "b"],
        chain_cursor=0,
        empty_streak=0,
        custom_predicate=lambda r: True,
    )
    assert decision.failover
    assert decision.reason == "custom_predicate"


def test_resolve_next_model_skips_unregistered() -> None:
    class _Reg:
        def __init__(self, ids: set[str]) -> None:
            self.ids = ids

        def require(self, model_id: str):  # type: ignore[no-untyped-def]
            if model_id in self.ids:
                return type("M", (), {"id": model_id})()
            raise KeyError(model_id)

    reg = _Reg({"a", "c"})  # b is missing → skip
    model, cursor = resolve_next_model(["a", "b", "c"], 0, reg)
    assert model is not None and model.id == "c"
    assert cursor == 2


def test_resolve_next_model_returns_none_when_none_available() -> None:
    class _Reg:
        def require(self, model_id: str):  # type: ignore[no-untyped-def]
            raise KeyError(model_id)

    model, cursor = resolve_next_model(["a", "b"], 0, _Reg())
    assert model is None
    assert cursor == 2


def test_overload_backoff_grows_then_caps() -> None:
    from oxenclaw.pi.run.run import (
        _OVERLOAD_FAILOVER_INITIAL_MS,
        _OVERLOAD_FAILOVER_MAX_MS,
        _overload_failover_backoff_seconds,
    )

    # Bounds: jittered ±20% around (initial * 2^(attempt-1)), capped at max.
    initial_s = _OVERLOAD_FAILOVER_INITIAL_MS / 1000.0
    max_s = _OVERLOAD_FAILOVER_MAX_MS / 1000.0

    # attempt=1 → base = 0.250s; jitter window [0.20, 0.30]
    d1 = _overload_failover_backoff_seconds(1)
    assert initial_s * 0.79 <= d1 <= initial_s * 1.21

    # attempt=10 (well past cap of 1500ms) — must respect ceiling+jitter
    d10 = _overload_failover_backoff_seconds(10)
    assert d10 <= max_s * 1.21
    # And not negative even with worst-case downward jitter
    assert d10 >= 0.0


async def test_run_loop_paces_failover_on_overload() -> None:
    """Rate-limit error: the run loop should pause before walking the chain.

    Drives a fake provider that always emits a retryable rate-limit error.
    We patch asyncio.sleep to capture the delay arguments and confirm the
    paced overload backoff fires (>= ~250ms first attempt, ignoring jitter).
    """
    import asyncio as _asyncio

    import oxenclaw.pi.providers  # noqa: F401
    from oxenclaw.pi import (
        InMemoryAuthStorage,
        Model,
        register_provider_stream,
        resolve_api,
    )
    from oxenclaw.pi.registry import InMemoryModelRegistry
    from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
    from oxenclaw.pi.streaming import ErrorEvent

    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # Provider-side overload: 429 retryable.
        yield ErrorEvent(message="rate limit exceeded (429)", retryable=True)

    register_provider_stream("overload_fake", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="head",
                provider="overload_fake",  # type: ignore[arg-type]
                max_output_tokens=64,
                extra={"base_url": "x"},
            ),
            Model(
                id="fallback",
                provider="overload_fake",  # type: ignore[arg-type]
                max_output_tokens=64,
                extra={"base_url": "x"},
            ),
        ]
    )
    head = reg.list()[0]
    api = await resolve_api(head, InMemoryAuthStorage({"overload_fake": "x"}))  # type: ignore[dict-item]

    sleep_delays: list[float] = []
    real_sleep = _asyncio.sleep

    async def fake_sleep(delay: float, *args, **kwargs):  # type: ignore[no-untyped-def]
        sleep_delays.append(delay)
        await real_sleep(0)

    cfg = RuntimeConfig(
        max_retries=1,
        backoff_initial=0.0,
        backoff_max=0.0,
        failover_chain=["fallback"],
        failover_registry=reg,
        # Wire a stub hook_runner exposing a `_auth` attribute the run loop
        # uses to re-resolve the api after switching models.
        hook_runner=type(
            "_HookRunner",
            (),
            {"_auth": InMemoryAuthStorage({"overload_fake": "x"})},
        )(),
    )
    _asyncio_sleep_orig = _asyncio.sleep
    _asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        await run_agent_turn(model=head, api=api, system=None, history=[], tools=[], config=cfg)
    finally:
        _asyncio.sleep = _asyncio_sleep_orig  # type: ignore[assignment]

    # At least one paced overload backoff should have fired (>= ~200ms even
    # with worst-case downward jitter on the 250ms initial).
    paced = [d for d in sleep_delays if d >= 0.18]
    assert paced, f"no paced backoff observed; sleeps={sleep_delays!r}"
