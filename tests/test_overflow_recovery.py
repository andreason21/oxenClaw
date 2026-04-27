"""Compress-then-retry self-heal tests.

Drive the run loop with a stub provider stream that emits 413 / 400-context-
overflow ErrorEvents and assert the loop:
  - bumps the per-turn `compression_self_heals` counter,
  - caps recovery attempts at `max_compression_self_heals`,
  - sets the long-context-tier flag on Anthropic-style messages.
"""

from __future__ import annotations

import oxenclaw.pi.providers  # noqa: F401  registers wrappers
from oxenclaw.pi import (
    Api,
    Model,
    StopEvent,
    TextDeltaEvent,
    register_provider_stream,
    text_message,
)
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.run.run import _maybe_record_long_context_tier
from oxenclaw.pi.streaming import ErrorEvent


def _model(provider: str = "test") -> Model:
    return Model(id="m", provider=provider, max_output_tokens=512)


def _api() -> Api:
    return Api(base_url="http://test")


async def test_413_triggers_compress_then_retry_then_succeeds() -> None:
    """First attempt yields 413, second attempt yields normal text."""
    state = {"calls": 0}

    async def flaky(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ErrorEvent(
                message="Request entity too large",
                retryable=True,
                status_code=413,
            )
            return
        yield TextDeltaEvent(delta="recovered")
        yield StopEvent(reason="end_turn")

    register_provider_stream("test_413_recover", flaky)

    cfg = RuntimeConfig(
        max_retries=0,  # force compress-then-retry path, not raw retry
        compress_then_retry=True,
        max_compression_self_heals=2,
        max_tool_iterations=4,
    )
    result = await run_agent_turn(
        model=_model("test_413_recover"),
        api=_api(),
        system=None,
        history=[text_message("hi")],
        tools=[],
        config=cfg,
    )
    assert state["calls"] == 2  # one failure, one success
    final = result.final_message
    assert any(getattr(b, "text", "") == "recovered" for b in final.content)


async def test_413_caps_self_heal_attempts(caplog) -> None:
    """When 413 keeps coming back, we bail after `max_compression_self_heals`."""
    import logging

    state = {"calls": 0}

    async def always_413(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield ErrorEvent(
            message="Request entity too large",
            retryable=True,
            status_code=413,
        )

    register_provider_stream("test_413_cap", always_413)

    cfg = RuntimeConfig(
        max_retries=0,
        backoff_initial=0.001,
        backoff_max=0.001,
        compress_then_retry=True,
        max_compression_self_heals=2,
        max_tool_iterations=10,
    )
    with caplog.at_level(logging.WARNING):
        result = await run_agent_turn(
            model=_model("test_413_cap"),
            api=_api(),
            system=None,
            history=[text_message("hi")],
            tools=[],
            config=cfg,
        )
    # Verify the compress-then-retry self-heal counter caps at 2 by
    # counting the structured warning the loop emits each time it
    # decides to break-and-retry. Silent transport retries are exempt.
    self_heal_logs = [
        rec for rec in caplog.records
        if "compress-then-retry" in rec.getMessage()
    ]
    assert len(self_heal_logs) == cfg.max_compression_self_heals
    assert result.stopped_reason == "error"


async def test_context_overflow_400_triggers_compress() -> None:
    """A 400 with context_length_exceeded should also self-heal."""
    state = {"calls": 0}

    async def flaky(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ErrorEvent(
                message="context_length_exceeded: prompt is too long",
                retryable=True,
                status_code=400,
            )
            return
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("test_400_overflow", flaky)

    cfg = RuntimeConfig(
        max_retries=0,
        compress_then_retry=True,
        max_compression_self_heals=2,
        max_tool_iterations=4,
    )
    result = await run_agent_turn(
        model=_model("test_400_overflow"),
        api=_api(),
        system=None,
        history=[text_message("hi")],
        tools=[],
        config=cfg,
    )
    assert state["calls"] == 2
    assert any(getattr(b, "text", "") == "ok" for b in result.final_message.content)


async def test_compress_then_retry_disabled_propagates_error(caplog) -> None:
    """When compress_then_retry=False, 413 never triggers a self-heal."""
    import logging

    state = {"calls": 0}

    async def stub(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield ErrorEvent(
            message="Request entity too large",
            retryable=True,
            status_code=413,
        )

    register_provider_stream("test_413_no_recovery", stub)

    cfg = RuntimeConfig(
        max_retries=0,
        backoff_initial=0.001,
        backoff_max=0.001,
        compress_then_retry=False,
        max_tool_iterations=4,
    )
    with caplog.at_level(logging.WARNING):
        result = await run_agent_turn(
            model=_model("test_413_no_recovery"),
            api=_api(),
            system=None,
            history=[text_message("hi")],
            tools=[],
            config=cfg,
        )
    # No structured "compress-then-retry" log entries should appear.
    self_heal_logs = [
        rec for rec in caplog.records
        if "compress-then-retry" in rec.getMessage()
    ]
    assert self_heal_logs == []
    assert result.stopped_reason == "error"


def test_long_context_tier_sets_session_flag() -> None:
    """Anthropic 'long context tier' message flips the force_200k flag."""
    cfg = RuntimeConfig()
    _maybe_record_long_context_tier(
        cfg,
        _model("anthropic"),
        "Your request exceeds the 200k context tier",
    )
    assert cfg.extra_params.get("force_200k_context") is True


def test_long_context_tier_no_match_no_flag() -> None:
    cfg = RuntimeConfig()
    _maybe_record_long_context_tier(
        cfg,
        _model("anthropic"),
        "some unrelated error",
    )
    assert "force_200k_context" not in cfg.extra_params


def test_long_context_tier_writes_to_hook_context_when_present() -> None:
    class _HookCtx:
        def __init__(self) -> None:
            self.session_flags: dict = {}

    hook_ctx = _HookCtx()
    cfg = RuntimeConfig(hook_context=hook_ctx)
    _maybe_record_long_context_tier(
        cfg,
        _model("anthropic"),
        "exceeds 200k context tier",
    )
    assert hook_ctx.session_flags.get("force_200k_context") is True
