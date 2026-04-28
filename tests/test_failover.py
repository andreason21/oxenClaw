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
