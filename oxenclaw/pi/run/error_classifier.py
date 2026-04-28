"""Structured API error classifier for the run loop.

Translates the inline status/message string-matching that used to live
in `run.py` into a single dispatch table that returns:

  - what kind of failure it was (`FailoverReason`),
  - whether it's safe to retry,
  - what self-healing action the loop should take next
    (compress context, rotate credential, walk failover chain),
  - and a `retry_after_seconds` hint when the provider supplied one.

Mirrors hermes-agent's `agent/error_classifier.py` but adapted to
oxenclaw: the input is a streaming `ErrorEvent` (already drained from
the provider stream), not a raw exception.  All matching is text-based
on `ErrorEvent.message` and integer-based on `ErrorEvent.status_code`,
so the classifier is provider-agnostic.

The run loop calls `classify_api_error(...)` once per attempt failure
and picks an action from the returned `ClassifiedError`:

  - `should_compress`  → break to the outer iteration so preemptive
                         compaction can trim context next round.
  - `should_rotate_credential` → ask the auth pool to cool the key
                                 down (best-effort; no-op if the pool
                                 isn't wired in).
  - `should_fallback`  → force-failover to the next chain entry.
  - terminal           → the existing terminal path runs.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oxenclaw.pi.streaming import ErrorEvent


class FailoverReason(enum.StrEnum):
    """Why an API call failed; selects the recovery strategy."""

    NONE = "none"
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    AUTH = "auth"
    SERVER = "server"
    CLIENT_ABORT = "client_abort"
    PROVIDER_BLOCKED = "provider_blocked"
    THINKING_SIGNATURE = "thinking_signature"
    EMPTY_RESPONSE = "empty_response"
    MODEL_NOT_FOUND = "model_not_found"
    CREDIT_EXHAUSTED = "credit_exhausted"
    UNKNOWN = "unknown"


@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""

    reason: FailoverReason
    retryable: bool
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    retry_after_seconds: float | None = None
    message: str = ""


# Pattern groups — ordered roughly by specificity.

_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "throttling",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "retry after",
    "resource_exhausted",
    "quota exceeded",
)

_CREDIT_EXHAUSTED_PATTERNS = (
    "insufficient credits",
    "insufficient_quota",
    "credit balance",
    "credits have been exhausted",
    "credit exhausted",
    "billing hard limit",
    "billing",
    "payment required",
    "exceeded your current quota",
)

_CONTEXT_OVERFLOW_PATTERNS = (
    "context length",
    "context_length_exceeded",
    "context size",
    "maximum context",
    "context window",
    "prompt is too long",
    "prompt exceeds max length",
    "prompt length",
    "input length",
    "input is too long",
    "max_model_len",
    "max input token",
    "exceeds the limit",
    "reduce the length",
    "too many tokens",
)

_PAYLOAD_TOO_LARGE_PATTERNS = (
    "request entity too large",
    "payload too large",
    "request too large",
    "error code: 413",
)

_AUTH_PATTERNS = (
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "forbidden",
    "authentication",
    "invalid token",
    "token expired",
    "token revoked",
    "access denied",
)

_MODEL_NOT_FOUND_PATTERNS = (
    "model not found",
    "model_not_found",
    "no such model",
    "unknown model",
    "invalid model",
    "is not a valid model",
    "does not exist",
    "unsupported model",
)

_PROVIDER_BLOCKED_PATTERNS = (
    "blocked by content policy",
    "policy_violation",
    "content policy",
    "safety policy",
    "no endpoints available matching your guardrail",
    "no endpoints available matching your data policy",
)

_THINKING_SIGNATURE_PATTERNS = (
    "thinking signature",
    "thinking block",
    "thinking_block",
    "invalid signature",
)

_TRANSPORT_PATTERNS = (
    "connection error",
    "connectionerror",
    "remoteprotocolerror",
    "timeout",
    "timed out",
    "server disconnected",
    "peer closed connection",
    "connection reset",
    "broken pipe",
    "incomplete chunked read",
    "unexpected eof",
)

_EMPTY_RESPONSE_PATTERNS = (
    "empty response",
    "no content",
    "empty stream",
    "no completion",
)

_CLIENT_ABORT_PATTERNS = (
    "aborted",
    "cancelled",
    "canceled",
    "client disconnected",
)


def _match_any(message: str, patterns: tuple[str, ...]) -> bool:
    return any(p in message for p in patterns)


# Heuristic threshold for "huge" approx_tokens — over this with a
# transport error, we probabilistically classify as context overflow
# instead of plain transient.
_CONTEXT_OVERFLOW_TOKEN_FLOOR = 120_000


def classify_api_error(
    *,
    error: ErrorEvent | None = None,
    status_code: int | None = None,
    message: str = "",
    approx_tokens: int | None = None,
    context_window: int | None = None,
    num_messages: int | None = None,
) -> ClassifiedError:
    """Classify an oxenclaw stream `ErrorEvent` into a recovery hint.

    The caller may pass either the `ErrorEvent` directly OR raw
    `status_code` / `message` (useful from places that built a synthetic
    error).  Optional `approx_tokens` / `context_window` / `num_messages`
    enable disambiguation heuristics for transport errors on large
    sessions.
    """
    # Pull fields off the ErrorEvent when given.
    if error is not None:
        status_code = status_code if status_code is not None else error.status_code
        msg = message or error.message or ""
        retry_after = error.retry_after_seconds
    else:
        msg = message or ""
        retry_after = None

    msg_lc = msg.lower()

    # ── Provider-specific patterns ──────────────────────────────────

    if "thinking" in msg_lc and ("signature" in msg_lc or "block" in msg_lc):
        return ClassifiedError(
            reason=FailoverReason.THINKING_SIGNATURE,
            retryable=True,
            should_compress=False,
            should_rotate_credential=False,
            should_fallback=False,
            retry_after_seconds=retry_after,
            message=msg,
        )

    # ── Status-code dispatch ────────────────────────────────────────

    if status_code is not None:
        if status_code == 401 or status_code == 403:
            return ClassifiedError(
                reason=FailoverReason.AUTH,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=False,
                retry_after_seconds=retry_after,
                message=msg,
            )
        if status_code == 402:
            return ClassifiedError(
                reason=FailoverReason.CREDIT_EXHAUSTED,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
                retry_after_seconds=retry_after,
                message=msg,
            )
        if status_code == 404 or _match_any(msg_lc, _MODEL_NOT_FOUND_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.MODEL_NOT_FOUND,
                retryable=False,
                should_fallback=True,
                retry_after_seconds=retry_after,
                message=msg,
            )
        if status_code == 413 or _match_any(msg_lc, _PAYLOAD_TOO_LARGE_PATTERNS):
            return ClassifiedError(
                reason=FailoverReason.PAYLOAD_TOO_LARGE,
                retryable=True,
                should_compress=True,
                retry_after_seconds=retry_after,
                message=msg,
            )
        if status_code == 429 or _match_any(msg_lc, _RATE_LIMIT_PATTERNS):
            # Rotate credential when the wait is long enough that this
            # key is effectively dead for the next call (>= 60s).
            rotate = bool(retry_after is not None and retry_after >= 60)
            return ClassifiedError(
                reason=FailoverReason.RATE_LIMIT,
                retryable=True,
                should_rotate_credential=rotate,
                should_fallback=rotate,
                retry_after_seconds=retry_after,
                message=msg,
            )
        if status_code == 400:
            # 400s are ambiguous: most are context overflow / format errors.
            if _match_any(msg_lc, _CONTEXT_OVERFLOW_PATTERNS):
                return ClassifiedError(
                    reason=FailoverReason.CONTEXT_OVERFLOW,
                    retryable=True,
                    should_compress=True,
                    retry_after_seconds=retry_after,
                    message=msg,
                )
            if _match_any(msg_lc, _PROVIDER_BLOCKED_PATTERNS):
                return ClassifiedError(
                    reason=FailoverReason.PROVIDER_BLOCKED,
                    retryable=False,
                    retry_after_seconds=retry_after,
                    message=msg,
                )
            if _match_any(msg_lc, _RATE_LIMIT_PATTERNS):
                return ClassifiedError(
                    reason=FailoverReason.RATE_LIMIT,
                    retryable=True,
                    should_rotate_credential=False,
                    should_fallback=False,
                    retry_after_seconds=retry_after,
                    message=msg,
                )
            # Generic 400: fall through to message-based classification
            # below; if nothing matches, emit UNKNOWN non-retryable.
        if 500 <= status_code < 600:
            return ClassifiedError(
                reason=FailoverReason.SERVER,
                retryable=True,
                retry_after_seconds=retry_after,
                message=msg,
            )

    # ── Message-based classification (no status code, or 400 fallthrough) ──

    if _match_any(msg_lc, _CREDIT_EXHAUSTED_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CREDIT_EXHAUSTED,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _PROVIDER_BLOCKED_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.PROVIDER_BLOCKED,
            retryable=False,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _CONTEXT_OVERFLOW_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _PAYLOAD_TOO_LARGE_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.PAYLOAD_TOO_LARGE,
            retryable=True,
            should_compress=True,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _RATE_LIMIT_PATTERNS):
        rotate = bool(retry_after is not None and retry_after >= 60)
        return ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=rotate,
            should_fallback=rotate,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _AUTH_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.AUTH,
            retryable=False,
            should_rotate_credential=True,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _MODEL_NOT_FOUND_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _CLIENT_ABORT_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.CLIENT_ABORT,
            retryable=False,
            retry_after_seconds=retry_after,
            message=msg,
        )
    if _match_any(msg_lc, _EMPTY_RESPONSE_PATTERNS):
        return ClassifiedError(
            reason=FailoverReason.EMPTY_RESPONSE,
            retryable=True,
            retry_after_seconds=retry_after,
            message=msg,
        )

    # Transport-level errors: connection close + huge session => context
    # overflow heuristic.  Otherwise treat as transient.
    if _match_any(msg_lc, _TRANSPORT_PATTERNS):
        is_huge = False
        if approx_tokens is not None:
            if approx_tokens >= _CONTEXT_OVERFLOW_TOKEN_FLOOR or (
                context_window is not None
                and context_window > 0
                and approx_tokens > int(context_window * 0.6)
            ):
                is_huge = True
        if num_messages is not None and num_messages > 200:
            is_huge = True
        if is_huge:
            return ClassifiedError(
                reason=FailoverReason.CONTEXT_OVERFLOW,
                retryable=True,
                should_compress=True,
                retry_after_seconds=retry_after,
                message=msg,
            )
        return ClassifiedError(
            reason=FailoverReason.TRANSIENT,
            retryable=True,
            retry_after_seconds=retry_after,
            message=msg,
        )

    # 400 fallthrough that didn't match any pattern → format error.
    if status_code == 400:
        return ClassifiedError(
            reason=FailoverReason.UNKNOWN,
            retryable=False,
            should_fallback=True,
            retry_after_seconds=retry_after,
            message=msg,
        )

    # Default: unknown but optimistically retryable.  Honour the
    # ErrorEvent's own `retryable` flag when we have it, since the
    # provider wrapper may know things we don't (e.g. status not
    # in our explicit table).
    retryable_default = bool(error.retryable) if error is not None else False
    return ClassifiedError(
        reason=FailoverReason.UNKNOWN,
        retryable=retryable_default,
        retry_after_seconds=retry_after,
        message=msg,
    )


__all__ = [
    "ClassifiedError",
    "FailoverReason",
    "classify_api_error",
]
