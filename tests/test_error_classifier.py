"""Structured error classifier — port of hermes-agent error_classifier.py.

Covers each FailoverReason taxonomy entry plus a round-trip test through
``run_agent_turn`` that exercises classifier-driven retry / compress /
fallback dispatch.
"""

from __future__ import annotations

import oxenclaw.pi.providers  # noqa: F401  registers wrappers
from oxenclaw.pi import (
    Api,
    Model,
    register_provider_stream,
    text_message,
)
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.run.error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)
from oxenclaw.pi.streaming import (
    ErrorEvent,
    StopEvent,
    TextDeltaEvent,
)

# ─── Reason taxonomy round-trip ─────────────────────────────────────


def test_classify_payload_too_large_413() -> None:
    err = ErrorEvent(message="HTTP 413: request too large", status_code=413)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.PAYLOAD_TOO_LARGE
    assert c.retryable
    assert c.should_compress
    assert isinstance(c, ClassifiedError)


def test_classify_payload_too_large_message_only() -> None:
    """No status code; message-text path must still classify."""
    c = classify_api_error(message="payload too large from upstream")
    assert c.reason is FailoverReason.PAYLOAD_TOO_LARGE
    assert c.should_compress


def test_classify_rate_limit_429_with_short_retry_after() -> None:
    err = ErrorEvent(
        message="HTTP 429: rate limit exceeded",
        status_code=429,
        retry_after_seconds=5.0,
    )
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.RATE_LIMIT
    assert c.retryable
    # Short retry-after → don't rotate the key prematurely.
    assert c.should_rotate_credential is False
    assert c.retry_after_seconds == 5.0


def test_classify_rate_limit_429_with_long_retry_after_rotates() -> None:
    err = ErrorEvent(
        message="HTTP 429: rate limit exceeded",
        status_code=429,
        retry_after_seconds=600.0,
    )
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.RATE_LIMIT
    assert c.should_rotate_credential is True
    assert c.should_fallback is True


def test_classify_context_overflow_400() -> None:
    err = ErrorEvent(
        message="HTTP 400: prompt is too long for this model",
        status_code=400,
    )
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.CONTEXT_OVERFLOW
    assert c.should_compress
    assert c.retryable


def test_classify_context_overflow_message_input_length() -> None:
    c = classify_api_error(message="error: input length exceeds limit")
    assert c.reason is FailoverReason.CONTEXT_OVERFLOW
    assert c.should_compress


def test_classify_auth_401_no_rotate_implies_terminal() -> None:
    err = ErrorEvent(message="HTTP 401: invalid api key", status_code=401)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.AUTH
    assert c.retryable is False
    assert c.should_rotate_credential is True


def test_classify_auth_403() -> None:
    err = ErrorEvent(message="HTTP 403: forbidden", status_code=403)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.AUTH
    assert c.retryable is False


def test_classify_server_error_500_502_503() -> None:
    for code in (500, 502, 503):
        err = ErrorEvent(message=f"HTTP {code}: internal", status_code=code)
        c = classify_api_error(error=err)
        assert c.reason is FailoverReason.SERVER, code
        assert c.retryable, code


def test_classify_thinking_signature_drops_signature() -> None:
    err = ErrorEvent(
        message="HTTP 400: thinking block signature invalid",
        status_code=400,
    )
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.THINKING_SIGNATURE
    assert c.retryable


def test_classify_model_not_found_404() -> None:
    err = ErrorEvent(message="HTTP 404: no such model", status_code=404)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.MODEL_NOT_FOUND
    assert c.should_fallback is True
    assert c.retryable is False


def test_classify_model_not_found_via_message() -> None:
    c = classify_api_error(message="Error: invalid model claude-foo")
    assert c.reason is FailoverReason.MODEL_NOT_FOUND
    assert c.should_fallback


def test_classify_credit_exhausted_402() -> None:
    err = ErrorEvent(message="HTTP 402: payment required", status_code=402)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.CREDIT_EXHAUSTED
    assert c.should_fallback is True
    assert c.retryable is False


def test_classify_session_expired_410() -> None:
    """Provider session/conversation handle no longer recognised — failover."""
    err = ErrorEvent(message="HTTP 410: session expired", status_code=410)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.SESSION_EXPIRED
    assert c.should_fallback is True
    assert c.retryable is False


def test_classify_credit_exhausted_via_billing_message() -> None:
    c = classify_api_error(message="Your credit balance is exhausted")
    assert c.reason is FailoverReason.CREDIT_EXHAUSTED


def test_classify_provider_blocked_content_policy() -> None:
    c = classify_api_error(message="Request blocked by content policy")
    assert c.reason is FailoverReason.PROVIDER_BLOCKED
    assert c.retryable is False


def test_classify_empty_response_via_message() -> None:
    c = classify_api_error(message="empty response from upstream")
    assert c.reason is FailoverReason.EMPTY_RESPONSE
    assert c.retryable


def test_classify_transient_transport() -> None:
    c = classify_api_error(message="connection error: server disconnected")
    assert c.reason is FailoverReason.TRANSIENT
    assert c.retryable


def test_classify_transport_with_huge_session_is_context_overflow() -> None:
    """A connection close on a large session is very likely overflow."""
    c = classify_api_error(
        message="server disconnected mid-stream",
        approx_tokens=180_000,
        context_window=200_000,
    )
    assert c.reason is FailoverReason.CONTEXT_OVERFLOW
    assert c.should_compress


def test_classify_client_abort() -> None:
    c = classify_api_error(message="request cancelled by client")
    assert c.reason is FailoverReason.CLIENT_ABORT
    assert c.retryable is False


def test_classify_unknown_default_non_retryable_400() -> None:
    """A 400 we don't recognise is unknown / non-retryable."""
    err = ErrorEvent(
        message="HTTP 400: schema mismatch on tool input",
        status_code=400,
    )
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.UNKNOWN
    assert c.retryable is False


def test_classify_unknown_falls_back_to_event_retryable_flag() -> None:
    """When nothing matches and there's no status, honour ErrorEvent.retryable."""
    err = ErrorEvent(message="something weird happened", retryable=True)
    c = classify_api_error(error=err)
    assert c.reason is FailoverReason.UNKNOWN
    assert c.retryable is True


# ─── Round-trip: run_agent_turn drives classifier dispatch ─────────


def _model(provider: str = "test") -> Model:
    return Model(id="m", provider=provider, max_output_tokens=512)


def _api() -> Api:
    return Api(base_url="http://test")


async def test_run_loop_classifier_terminal_on_provider_blocked() -> None:
    """A PROVIDER_BLOCKED stream error must terminate immediately."""

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        yield ErrorEvent(
            message="HTTP 400: blocked by content policy",
            status_code=400,
            retryable=False,
        )

    register_provider_stream("classifier_blocked", fake)
    out = await run_agent_turn(
        model=_model("classifier_blocked"),
        api=_api(),
        system=None,
        history=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(max_retries=2),
    )
    assert out.stopped_reason == "error"


async def test_run_loop_classifier_recovers_after_transient() -> None:
    """First call errors transiently; second call succeeds."""

    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ErrorEvent(
                message="HTTP 503: overloaded",
                status_code=503,
                retryable=True,
            )
            return
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("classifier_transient", fake)
    out = await run_agent_turn(
        model=_model("classifier_transient"),
        api=_api(),
        system=None,
        history=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(
            max_retries=3,
            backoff_initial=0.001,
            backoff_max=0.002,
        ),
    )
    assert out.stopped_reason == "end_turn"
    assert state["calls"] == 2


async def test_run_loop_classifier_compress_then_retry_breaks_to_outer() -> None:
    """A context-overflow error triggers compress-then-retry: the inner
    while loop breaks and the outer for-loop iterates again. The next
    attempt then succeeds."""

    state = {"calls": 0}

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ErrorEvent(
                message="HTTP 400: prompt is too long",
                status_code=400,
                retryable=False,
            )
            return
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("classifier_compress", fake)
    out = await run_agent_turn(
        model=_model("classifier_compress"),
        api=_api(),
        system=None,
        history=[text_message("hi")],
        tools=[],
        config=RuntimeConfig(
            max_retries=1,
            backoff_initial=0.001,
            backoff_max=0.002,
            preemptive_compaction=False,  # avoid noise from the unrelated path
            compress_then_retry=True,
        ),
    )
    assert state["calls"] == 2
    assert out.stopped_reason == "end_turn"
