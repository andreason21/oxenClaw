"""Phase 9: extras tests."""

from __future__ import annotations

import asyncio

from sampyclaw.pi import (
    AssistantMessage,
    Model,
    SystemMessage,
    TextContent,
    ThinkingBlock,
    UserMessage,
)
from sampyclaw.pi.extras import (
    LaneRouter,
    UsageAccumulator,
    cancel_on,
    classify_failure,
    merge_extra_params,
    rewrite_transcript,
    select_failover_model,
    summarize_usage,
    wait_for_idle,
)


# ─── classifier ──────────────────────────────────────────────────────


def test_classify_failure_categories() -> None:
    assert classify_failure("HTTP 429 rate limited") == "rate_limit"
    assert classify_failure("invalid api key") == "auth"
    assert classify_failure("context length exceeded for model") == "context_overflow"
    assert classify_failure("Read timed out") == "transient"
    assert classify_failure("HTTP 503 Service Unavailable") == "transient"
    assert classify_failure("400 bad request") == "client_error"
    assert classify_failure("nope") == "unknown"
    assert classify_failure("") == "unknown"


# ─── failover ────────────────────────────────────────────────────────


def test_failover_prefers_same_provider() -> None:
    primary = Model(id="claude-sonnet-4-6", provider="anthropic", context_window=1_000_000)
    pool = [
        primary,
        Model(id="claude-haiku-4-5", provider="anthropic", context_window=200_000),
        Model(id="gpt-4o", provider="openai", context_window=128_000),
    ]
    pick = select_failover_model(primary, pool)
    assert pick is not None and pick.id == "claude-haiku-4-5"


def test_failover_falls_back_to_other_provider_when_no_same() -> None:
    primary = Model(id="x", provider="anthropic", context_window=200_000)
    pool = [primary, Model(id="gpt-4o", provider="openai", context_window=200_000)]
    pick = select_failover_model(primary, pool)
    assert pick is not None and pick.id == "gpt-4o"


def test_failover_returns_none_when_no_pool() -> None:
    primary = Model(id="solo", provider="anthropic")
    assert select_failover_model(primary, [primary]) is None


# ─── lane router ─────────────────────────────────────────────────────


async def test_lane_router_caps_concurrency() -> None:
    router = LaneRouter(default_cap=2)
    started = 0
    holding = asyncio.Event()
    started_evt = asyncio.Event()

    async def worker() -> None:
        nonlocal started
        await router.acquire("anthropic")
        started += 1
        if started == 2:
            started_evt.set()
        try:
            await holding.wait()
        finally:
            router.release("anthropic")

    tasks = [asyncio.create_task(worker()) for _ in range(4)]
    await asyncio.wait_for(started_evt.wait(), timeout=1.0)
    assert started == 2  # only the first 2 cleared the semaphore
    holding.set()
    await asyncio.gather(*tasks)


# ─── usage accumulator ──────────────────────────────────────────────


def test_usage_accumulator_aggregates_and_costs() -> None:
    acc = UsageAccumulator()
    acc.add({"input_tokens": 1000, "output_tokens": 500})
    acc.add({"input_tokens": 200}, pricing={"input_tokens": 3.0})
    out = acc.summarize()
    assert out["input_tokens"] == 1200
    assert out["output_tokens"] == 500
    # 1200 / 1M * 3 = 0.0036
    assert abs(out["cost_usd"] - 0.0036) < 1e-9


def test_summarize_usage_helper() -> None:
    summary = summarize_usage(
        [{"input_tokens": 100}, {"input_tokens": 200, "output_tokens": 50}]
    )
    assert summary["input_tokens"] == 300
    assert summary["output_tokens"] == 50


# ─── transcript rewrite ─────────────────────────────────────────────


def test_rewrite_redacts_tokens_in_user_text() -> None:
    msgs = [UserMessage(content="my key is sk-abcdef")]
    out = rewrite_transcript(msgs, redact_tokens=("sk-abcdef",))
    assert "[REDACTED]" in out[0].content  # type: ignore[union-attr]
    assert "sk-abcdef" not in out[0].content  # type: ignore[union-attr]


def test_rewrite_drops_thinking_blocks() -> None:
    msgs = [
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="hidden chain"),
                TextContent(text="visible answer"),
            ],
            stop_reason="end_turn",
        ),
    ]
    out = rewrite_transcript(msgs, drop_thinking=True)
    types = [b.type for b in out[0].content]  # type: ignore[union-attr]
    assert "thinking" not in types
    assert "text" in types


def test_rewrite_does_not_mutate_input() -> None:
    msg = UserMessage(content="secret-token-zzz")
    rewrite_transcript([msg], redact_tokens=("secret-token-zzz",))
    assert msg.content == "secret-token-zzz"


# ─── merge_extra_params + wait_for_idle + cancel_on ─────────────────


def test_merge_extra_params_right_wins_and_skips_none() -> None:
    out = merge_extra_params({"a": 1, "b": 2}, None, {"b": 3, "c": 4})
    assert out == {"a": 1, "b": 3, "c": 4}


async def test_wait_for_idle_returns_promptly() -> None:
    import time

    t0 = time.monotonic()
    await wait_for_idle(0.01)
    assert time.monotonic() - t0 < 0.5


async def test_cancel_on_resolves_when_event_set() -> None:
    ev = asyncio.Event()
    fut = cancel_on(ev)
    assert not fut.done()
    ev.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert fut.done()


async def test_cancel_on_already_set_resolves_immediately() -> None:
    ev = asyncio.Event()
    ev.set()
    fut = cancel_on(ev)
    assert fut.done()
