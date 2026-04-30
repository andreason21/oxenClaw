"""Cyclic failover tests."""

from __future__ import annotations

from oxenclaw.pi.messages import AssistantMessage, TextContent
from oxenclaw.pi.models import Model
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.run.attempt import AttemptResult
from oxenclaw.pi.run.failover import resolve_next_model, should_failover
from oxenclaw.pi.streaming import ErrorEvent


def _err_result() -> AttemptResult:
    return AttemptResult(
        message=AssistantMessage(content=[TextContent(text="")], stop_reason="error"),
        error=ErrorEvent(message="provider_error", retryable=True),
    )


def _registry(ids: list[str]) -> InMemoryModelRegistry:
    return InMemoryModelRegistry(
        models=[
            Model(id=mid, provider="x", max_output_tokens=64, extra={"base_url": "u"})
            for mid in ids
        ]
    )


def test_should_failover_no_cycle_at_tail() -> None:
    """Default behaviour: at tail, refuse to failover."""
    decision = should_failover(
        result=_err_result(),
        chain=["a", "b"],
        chain_cursor=1,  # at tail
        empty_streak=0,
    )
    assert not decision.failover
    assert decision.reason == "end_of_chain"


def test_should_failover_cycle_at_tail_first_pass() -> None:
    """cycle=True with cycles_used=0: wrap to head."""
    decision = should_failover(
        result=_err_result(),
        chain=["a", "b"],
        chain_cursor=1,
        empty_streak=0,
        cycle=True,
        cycles_used=0,
    )
    assert decision.failover
    assert decision.next_model_id == "a"


def test_should_failover_cycle_exhausted() -> None:
    """cycles_used >= len(chain): refuse further wraps."""
    decision = should_failover(
        result=_err_result(),
        chain=["a", "b"],
        chain_cursor=1,
        empty_streak=0,
        cycle=True,
        cycles_used=2,  # already wrapped len(chain) times
    )
    assert not decision.failover
    assert decision.reason == "end_of_chain"


def test_resolve_next_model_wraps_to_head() -> None:
    reg = _registry(["a", "b", "c"])
    model, cursor = resolve_next_model(
        ["a", "b", "c"], cursor=2, registry=reg, cycle=True, cycles_used=0
    )
    assert model is not None
    assert model.id == "a"
    assert cursor == 0


def test_resolve_next_model_no_wrap_when_cycle_off() -> None:
    reg = _registry(["a", "b"])
    model, cursor = resolve_next_model(
        ["a", "b"], cursor=1, registry=reg, cycle=False, cycles_used=0
    )
    assert model is None
    assert cursor == 2


def test_resolve_next_model_skips_unregistered_in_cycle() -> None:
    """Unregistered chain entries are skipped during a wrap."""
    # cursor is at "tail" (idx=2). Forward exhausts chain → wrap to 0
    # ("typo", unregistered) → skip → next idx 1 ("good", registered).
    reg = _registry(["good", "tail"])
    model, _cursor = resolve_next_model(
        ["typo", "good", "tail"], cursor=2, registry=reg, cycle=True, cycles_used=0
    )
    assert model is not None
    assert model.id == "good"


def test_runtime_config_failover_cycle_default_off() -> None:
    from oxenclaw.pi.run.runtime import RuntimeConfig

    rc = RuntimeConfig()
    assert rc.failover_cycle is False
