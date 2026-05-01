"""Plan-then-execute orchestrator: plan parsing + verifier verdict + flow."""

from __future__ import annotations

from typing import Any

import oxenclaw.pi.providers  # noqa: F401  — registers stream wrappers
from oxenclaw.agents.plan_execute import (
    extract_numbered_steps,
    parse_verdict,
    run_plan_then_execute,
)
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.run import RuntimeConfig
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent

# ────────────────────────────────────────────────────────────────────
# Plan parsing
# ────────────────────────────────────────────────────────────────────


def test_extract_numbered_steps_basic() -> None:
    text = "1. Fetch the data\n2. Summarize it\n3. Reply"
    assert extract_numbered_steps(text) == [
        "Fetch the data",
        "Summarize it",
        "Reply",
    ]


def test_extract_numbered_steps_tolerates_paren_and_space() -> None:
    text = "1) first step\n2) second\n3)  third  "
    assert extract_numbered_steps(text) == ["first step", "second", "third"]


def test_extract_numbered_steps_ignores_prose_and_inner_numbers() -> None:
    text = (
        "Here's the plan:\n"
        "1. Plan A\n"
        "2. Plan B (which has 1. some sub-detail)\n"
        "Final notes follow.\n"
        "3. Plan C\n"
    )
    steps = extract_numbered_steps(text)
    # The "1. some sub-detail" inside the line for step 2 doesn't
    # appear at line-head so it doesn't match. Steps 1/2/3 survive.
    assert steps == ["Plan A", "Plan B (which has 1. some sub-detail)", "Plan C"]


def test_extract_numbered_steps_empty_when_no_numbers() -> None:
    assert extract_numbered_steps("Just prose, no plan here.") == []
    assert extract_numbered_steps("") == []


# ────────────────────────────────────────────────────────────────────
# Verifier verdict
# ────────────────────────────────────────────────────────────────────


def test_parse_verdict_yes_with_colon() -> None:
    v = parse_verdict("YES: file fetched and parsed")
    assert v.met is True
    assert "fetched" in v.reason


def test_parse_verdict_no_with_colon() -> None:
    v = parse_verdict("NO: tool returned an error")
    assert v.met is False
    assert "error" in v.reason


def test_parse_verdict_yes_with_dash() -> None:
    assert parse_verdict("Yes - looks good").met is True
    assert parse_verdict("yes").met is True


def test_parse_verdict_no_with_lower_case() -> None:
    assert parse_verdict("no, the data is empty").met is False


def test_parse_verdict_leading_prose() -> None:
    text = "Let me check.\nYES: the answer matches the expected format"
    assert parse_verdict(text).met is True


def test_parse_verdict_ambiguous_defaults_no() -> None:
    """Unparseable text → met=False so we err toward retry."""
    v = parse_verdict("hmm interesting")
    assert v.met is False


def test_parse_verdict_empty_defaults_no() -> None:
    assert parse_verdict("").met is False
    assert parse_verdict("   \n  ").met is False


# ────────────────────────────────────────────────────────────────────
# End-to-end flow with scripted provider
# ────────────────────────────────────────────────────────────────────


class _ScriptedProvider:
    """Yields canned text replies in the order they were registered.

    Each call to the registered stream consumes one entry from the
    queue. This lets a test pin every turn (planner, step 1 attempt 1,
    step 1 verifier, step 2 attempt 1, step 2 verifier, ...).
    """

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def stream(self, ctx, opts):  # type: ignore[no-untyped-def]
        self.calls += 1
        text = "[script exhausted]" if not self._replies else self._replies.pop(0)
        if text:
            yield TextDeltaEvent(delta=text)
        yield StopEvent(reason="end_turn")


async def _build_model(provider_id: str, replies: list[str]) -> tuple[Any, Any, _ScriptedProvider]:
    sp = _ScriptedProvider(replies)
    register_provider_stream(provider_id, sp.stream)
    model = Model(
        id="m",
        provider=provider_id,  # type: ignore[arg-type]
        max_output_tokens=512,
        extra={"base_url": "x"},
    )
    api = await resolve_api(
        model,
        InMemoryAuthStorage({provider_id: "x"}),  # type: ignore[dict-item]
    )
    return model, api, sp


async def test_run_plan_then_execute_happy_path() -> None:
    """3-step plan, validator YES, every step verified YES on first attempt."""
    replies = [
        # Phase 1: planning
        "1. Step alpha\n2. Step beta\n3. Step gamma",
        # Phase 1: plan-validator
        "YES: plan looks sound",
        # Phase 2: step 1 attempt
        "Did alpha.",
        # Phase 2: step 1 verifier
        "YES: alpha complete",
        # Phase 2: step 2 attempt
        "Did beta.",
        # Phase 2: step 2 verifier
        "YES: beta complete",
        # Phase 2: step 3 attempt
        "Did gamma.",
        # Phase 2: step 3 verifier
        "YES: gamma complete",
    ]
    model, api, sp = await _build_model("plan_happy", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="please do alpha beta gamma",
        model=model,
        api=api,
        system="You are a test harness.",
        tools=[],
        config=cfg,
    )
    assert out.fell_back_to_single_call is False
    assert out.plan_steps == ["Step alpha", "Step beta", "Step gamma"]
    assert out.plan_attempts == 1
    assert out.plan_verdict_met is True
    assert len(out.step_runs) == 3
    assert all(r.verifier_met for r in out.step_runs)
    assert all(r.attempts == 1 for r in out.step_runs)
    # 1 plan + 1 plan-validator + 3 × (attempt + verifier) = 8 turns
    assert out.total_turns == 8
    assert sp.calls == 8


async def test_run_plan_then_execute_retries_on_no() -> None:
    """Step 2 fails verifier once, retries, succeeds."""
    replies = [
        "1. one\n2. two\n3. three",  # plan
        "YES: ok",  # plan-validator
        "Did one.",  # step 1 attempt
        "YES: one ok",  # step 1 verifier
        "Tried two.",  # step 2 attempt 1
        "NO: missed the constraint",  # step 2 verifier 1 → retry
        "Did two correctly.",  # step 2 attempt 2
        "YES: two done",  # step 2 verifier 2
        "Did three.",  # step 3 attempt
        "YES: three done",  # step 3 verifier
    ]
    model, api, _ = await _build_model("plan_retry", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="do one two three",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
        verifier_retry_cap=2,
    )
    assert out.step_runs[1].attempts == 2
    assert out.step_runs[1].verifier_met is True
    # 1 plan + 1 plan-validator + step1(2) + step2(4) + step3(2) = 10
    assert out.total_turns == 10


async def test_run_plan_then_execute_gives_up_after_cap() -> None:
    """Verifier keeps saying NO; helper stops after retry cap."""
    replies = [
        "1. only\n2. two",  # plan
        "YES: ok",  # plan-validator
        "tried.",  # step 1 attempt 1
        "NO: bad",  # verifier 1 → retry
        "tried again.",  # step 1 attempt 2
        "NO: still bad",  # verifier 2 → retry
        "tried once more.",  # step 1 attempt 3 (cap=2 → 3 total)
        "NO: still wrong",  # verifier 3
        # Step 2 still runs after step 1 gives up
        "did two.",
        "YES: two ok",
    ]
    model, api, _ = await _build_model("plan_giveup", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="do only and two",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
        verifier_retry_cap=2,
    )
    assert out.step_runs[0].attempts == 3
    assert out.step_runs[0].verifier_met is False
    assert out.step_runs[1].verifier_met is True


async def test_run_plan_then_execute_falls_back_when_no_plan() -> None:
    """Planner returns prose with no numbers — fall back to single call."""
    replies = [
        "I'd just answer directly without a plan.",  # plan phase: no numbers
        "Hello!",  # single-call fallback
    ]
    model, api, _ = await _build_model("plan_fallback", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="hi",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
    )
    assert out.fell_back_to_single_call is True
    assert out.plan_steps == []
    assert "Hello!" in out.final_text
    # 1 plan attempt + 1 single-call fallback (no validator runs when
    # the plan can't be parsed)
    assert out.total_turns == 2


async def test_plan_validator_rejects_then_replans() -> None:
    """Validator NO on first plan, second plan accepted, executor proceeds."""
    replies = [
        # Phase 1: planning attempt 1 — pre-computes the answer
        "1. Multiply 2 by 3 to get 6.\n2. Output 6.",
        # Phase 1: validator on attempt 1
        "NO: step 1 embeds the literal expected output (6)",
        # Phase 1: planning attempt 2 — clean
        "1. Multiply 2 by 3.\n2. Output the product.",
        # Phase 1: validator on attempt 2
        "YES: action-only, no pre-compute",
        # Phase 2: step 1
        "Multiplied to get 6.",
        "YES: product computed",
        # Phase 2: step 2
        "6",
        "YES: outputted",
    ]
    model, api, sp = await _build_model("plan_validator_retry", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="multiply 2 and 3",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
        plan_retry_cap=1,
    )
    assert out.plan_attempts == 2
    assert out.plan_verdict_met is True
    assert "Multiply 2 by 3" in out.plan_steps[0]
    assert out.plan_steps == ["Multiply 2 by 3.", "Output the product."]
    # 2 plan turns + 2 validator turns + 2 × (step + verifier) = 8
    assert out.total_turns == 8
    assert sp.calls == 8


async def test_plan_validator_gives_up_after_cap_but_proceeds() -> None:
    """Validator keeps saying NO; we stop replanning at cap and execute the last plan anyway."""
    replies = [
        "1. one\n2. two",  # plan attempt 1
        "NO: too vague",  # validator 1
        "1. one again\n2. two again",  # plan attempt 2 (cap=1 → 2 total)
        "NO: still vague",  # validator 2 — give up, proceed with this plan
        # Phase 2 runs against the last plan
        "Did one.",
        "YES: ok",
        "Did two.",
        "YES: ok",
    ]
    model, api, _ = await _build_model("plan_validator_giveup", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="do it",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
        plan_retry_cap=1,
    )
    assert out.plan_attempts == 2
    assert out.plan_verdict_met is False
    assert out.plan_verdict_reason.startswith("still vague")
    # Execution still happened against the last plan.
    assert out.plan_steps == ["one again", "two again"]
    assert all(r.verifier_met for r in out.step_runs)
    # 2 plan turns + 2 validator turns + 2 × (step + verifier) = 8
    assert out.total_turns == 8


async def test_plan_validator_disabled_with_cap_zero_runs_validator_once() -> None:
    """plan_retry_cap=0 still runs the validator — just no replanning attempt."""
    replies = [
        "1. one\n2. two",  # plan
        "NO: too vague",  # validator (no retry left)
        "Did one.",
        "YES: ok",
        "Did two.",
        "YES: ok",
    ]
    model, api, _ = await _build_model("plan_validator_no_retry", replies)
    cfg = RuntimeConfig()
    out = await run_plan_then_execute(
        user_request="do it",
        model=model,
        api=api,
        system="harness",
        tools=[],
        config=cfg,
        plan_retry_cap=0,
    )
    assert out.plan_attempts == 1
    assert out.plan_verdict_met is False
    # 1 plan + 1 validator + 2 × (step + verifier) = 6
    assert out.total_turns == 6
