"""Plan-then-execute orchestrator for local-LLM agent runs.

A standalone helper around `oxenclaw.pi.run.run_agent_turn`. For local
7-13B models the bundled PiAgent flow ("plan + execute in one inference
loop") often fails because the model emits tool calls mid-thought
before the plan is settled. Forcing two phases — write the plan first
WITHOUT tools, then execute steps one at a time WITH tools and a
verifier turn between them — typically lifts success rate 2-3× on
qwen3.5:9b / llama3.1:8b for multi-step tasks.

Public API:

  result = await run_plan_then_execute(
      user_request="...",
      model=model,
      api=api,
      system="...",
      tools=tools,
      config=runtime_config,
      verifier_retry_cap=2,
  )

The helper is deliberately stateless — callers (PiAgent or a future
PlanExecuteAgent wrapper) own session persistence and message
appending.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.plan_execute")

# Plan-extraction regex: tolerates "1. step", "1) step", "1 step",
# "**1.** step", with optional leading bullet/dash. Must start with
# a digit anchored at line head (after optional whitespace + bullets).
_NUMBERED_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?\s*(\d+)[.)\s]\s*(?:\*\*)?\s*(.+?)\s*$")


def extract_numbered_steps(plan_text: str) -> list[str]:
    """Parse a numbered plan ("1. ...\\n2. ...") into a list of step bodies.

    Returns the steps in document order. Empty list when no numbered
    structure is found — the caller should fall back to single-call
    execution in that case.
    """
    steps: list[tuple[int, str]] = []
    for line in plan_text.splitlines():
        m = _NUMBERED_LINE_RE.match(line)
        if m:
            n = int(m.group(1))
            body = m.group(2).strip()
            if body:
                steps.append((n, body))
    if not steps:
        return []
    # De-duplicate accidental matches (e.g. nested "1." inside a body)
    # by keeping only entries whose number is strictly increasing —
    # tolerates one out-of-order line but cuts the run when the plan
    # text wraps into prose.
    out: list[str] = []
    last_n = 0
    for n, body in steps:
        if n <= last_n:
            continue
        out.append(body)
        last_n = n
    return out


def _extract_text(message: AssistantMessage) -> str:
    """Pull all TextContent blocks out of an assistant message."""
    parts: list[str] = []
    for b in message.content:
        if isinstance(b, TextContent) and b.text:
            parts.append(b.text)
    return "\n".join(parts).strip()


@dataclass(frozen=True)
class Verdict:
    """Result of the verifier turn."""

    met: bool
    reason: str


def parse_verdict(text: str) -> Verdict:
    """Parse a verifier reply.

    Tolerated shapes (case-insensitive):
      - "YES: <reason>"
      - "NO: <reason>"
      - "Yes - <reason>"
      - bare "Yes" / "No" with a one-line explanation following
      - leading prose then a YES/NO line

    Defaults to `met=False` on ambiguous output so we err toward
    retrying rather than declaring premature success.
    """
    cleaned = text.strip()
    if not cleaned:
        return Verdict(met=False, reason="(empty verifier reply)")
    # Look for first line starting with yes/no (after optional bullets).
    for raw_line in cleaned.splitlines():
        line = raw_line.strip().lstrip("-*•").strip()
        low = line.lower()
        if low.startswith("yes"):
            tail = line.split(":", 1)[-1].strip() if ":" in line else line[3:].strip(" -—")
            return Verdict(met=True, reason=tail or "yes")
        if low.startswith("no"):
            tail = line.split(":", 1)[-1].strip() if ":" in line else line[2:].strip(" -—")
            return Verdict(met=False, reason=tail or "no")
    # Fallback: scan first 80 chars for unambiguous yes/no.
    head = cleaned.lower()[:80]
    if "yes" in head and "no" not in head:
        return Verdict(met=True, reason=cleaned[:120])
    if "no" in head and "yes" not in head:
        return Verdict(met=False, reason=cleaned[:120])
    return Verdict(met=False, reason=f"(ambiguous: {cleaned[:80]!r})")


@dataclass
class StepRun:
    """Outcome of a single step in the execute phase."""

    step_text: str
    attempts: int = 0
    final_text: str = ""
    verifier_met: bool = False
    verifier_reason: str = ""


@dataclass
class PlanExecuteResult:
    """Aggregate result of a plan-then-execute run."""

    plan_text: str
    plan_steps: list[str] = field(default_factory=list)
    step_runs: list[StepRun] = field(default_factory=list)
    final_text: str = ""
    fell_back_to_single_call: bool = False
    total_turns: int = 0  # planning + per-step attempts + verifier turns


_PLANNING_GUIDANCE = (
    "\n\n────── PLANNING PHASE ──────\n"
    "For THIS turn ONLY, do not call any tools. Produce a numbered "
    "plan (3-7 actionable steps) that fulfils the user's request. "
    "Format strictly as:\n"
    "1. <step>\n2. <step>\n3. <step>\n"
    "Each step should be concrete enough that an executor can do it "
    "with one or two tool calls. Reply with the plan only — no "
    "preamble, no commentary."
)

_STEP_GUIDANCE_TEMPLATE = (
    "\n\n────── EXECUTION PHASE — STEP {n}/{total} ──────\n"
    "Now execute step {n}: {step}\n"
    "Use the available tools as needed. When done with this step, "
    "give a one-line summary of what was accomplished."
)

_VERIFIER_GUIDANCE_TEMPLATE = (
    "Did the previous turn satisfy step {n} of the plan? "
    "Step {n}: {step}\n"
    "Reply EXACTLY in this format on a single line:\n"
    "  YES: <one-line reason>\n"
    "or\n"
    "  NO: <one-line reason>\n"
    "Be strict — only answer YES if the step is genuinely complete "
    "(not just attempted)."
)


async def run_plan_then_execute(
    *,
    user_request: str,
    model: Any,
    api: Any,
    system: str,
    tools: list[Any],
    config: RuntimeConfig,
    history: Iterable[Any] | None = None,
    verifier_retry_cap: int = 2,
) -> PlanExecuteResult:
    """Two-phase orchestrator. See module docstring.

    Returns a `PlanExecuteResult` aggregating the plan, every per-step
    outcome (with attempts and verifier verdict), and a flat
    `final_text` suitable for the dashboard channel.

    `history` is the prior conversation; we don't mutate it. Per-step
    history accumulates the model's own responses so subsequent steps
    have context (and the verifier can reference what happened).
    """
    base_history: list[Any] = list(history or [])
    out = PlanExecuteResult(plan_text="")

    # ── Phase 1: planning (tools disabled) ───────────────────────────
    plan_system = system + _PLANNING_GUIDANCE
    plan_history = [
        *base_history,
        UserMessage(content=f"User request: {user_request}\n\nProduce the plan."),
    ]
    plan_turn = await run_agent_turn(
        model=model,
        api=api,
        system=plan_system,
        history=plan_history,
        tools=[],
        config=config,
    )
    out.total_turns += 1
    out.plan_text = _extract_text(plan_turn.final_message)
    out.plan_steps = extract_numbered_steps(out.plan_text)

    if not out.plan_steps:
        # Fallback: model didn't produce a parseable plan. Run the
        # request as a single call so we don't strand the user.
        logger.warning(
            "plan-execute: no parseable steps from plan text (len=%d) — falling back to single call",
            len(out.plan_text),
        )
        single_history = [*base_history, UserMessage(content=user_request)]
        single = await run_agent_turn(
            model=model,
            api=api,
            system=system,
            history=single_history,
            tools=tools,
            config=config,
        )
        out.total_turns += 1
        out.fell_back_to_single_call = True
        out.final_text = _extract_text(single.final_message)
        return out

    # ── Phase 2: per-step execution + verifier ───────────────────────
    accumulated: list[Any] = list(base_history)
    accumulated.append(UserMessage(content=f"User request: {user_request}"))
    accumulated.append(
        AssistantMessage(
            content=[TextContent(text=f"Plan:\n{out.plan_text}")],
            stop_reason="end_turn",
        )
    )

    total_steps = len(out.plan_steps)
    for i, step in enumerate(out.plan_steps, start=1):
        run = StepRun(step_text=step)
        out.step_runs.append(run)

        step_history = list(accumulated)
        step_history.append(
            UserMessage(content=_STEP_GUIDANCE_TEMPLATE.format(n=i, total=total_steps, step=step))
        )

        # Per-step retry loop (capped). Verifier "no" → retry the step.
        for attempt_idx in range(verifier_retry_cap + 1):
            run.attempts += 1
            step_turn = await run_agent_turn(
                model=model,
                api=api,
                system=system,
                history=step_history,
                tools=tools,
                config=config,
            )
            out.total_turns += 1
            run.final_text = _extract_text(step_turn.final_message)
            # Roll the step turn into our running history for the
            # verifier (and for subsequent steps).
            step_history = [*step_history, *step_turn.appended_messages]

            # Verifier turn — tools disabled to keep it cheap and
            # deterministic. The model only judges, doesn't act.
            verifier_history = [
                *step_history,
                UserMessage(content=_VERIFIER_GUIDANCE_TEMPLATE.format(n=i, step=step)),
            ]
            verifier_turn = await run_agent_turn(
                model=model,
                api=api,
                system=system,
                history=verifier_history,
                tools=[],
                config=config,
            )
            out.total_turns += 1
            verdict = parse_verdict(_extract_text(verifier_turn.final_message))
            run.verifier_met = verdict.met
            run.verifier_reason = verdict.reason

            if verdict.met:
                logger.info(
                    "plan-execute: step %d/%d ✓ (attempt %d/%d) — %s",
                    i,
                    total_steps,
                    attempt_idx + 1,
                    verifier_retry_cap + 1,
                    verdict.reason[:80],
                )
                break
            logger.warning(
                "plan-execute: step %d/%d ✗ (attempt %d/%d) — %s",
                i,
                total_steps,
                attempt_idx + 1,
                verifier_retry_cap + 1,
                verdict.reason[:80],
            )
            # Append a synthetic user nudge for the retry attempt so
            # the model sees the verifier's complaint.
            if attempt_idx < verifier_retry_cap:
                step_history = [
                    *step_history,
                    UserMessage(
                        content=(
                            f"The previous attempt did not satisfy step {i}. "
                            f"Reason: {verdict.reason}\n"
                            f"Retry step {i}: {step}"
                        )
                    ),
                ]

        accumulated = step_history

    out.final_text = "\n\n".join(
        _format_step_summary(i + 1, sr) for i, sr in enumerate(out.step_runs)
    )
    return out


def _format_step_summary(idx: int, run: StepRun) -> str:
    icon = "✓" if run.verifier_met else "⚠"
    suffix = (
        f"  {icon} {run.verifier_reason}"
        if run.verifier_reason
        else f"  {icon} (no verifier reason)"
    )
    body = run.final_text or "(no text emitted)"
    if run.attempts > 1:
        return f"[Step {idx}] (×{run.attempts}) {body}\n{suffix}"
    return f"[Step {idx}] {body}\n{suffix}"


__all__ = [
    "PlanExecuteResult",
    "StepRun",
    "Verdict",
    "extract_numbered_steps",
    "parse_verdict",
    "run_plan_then_execute",
]
