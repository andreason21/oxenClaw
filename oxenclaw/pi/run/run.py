"""Multi-attempt run loop.

Translates openclaw `pi-embedded-runner/run.ts` (~2.3K LOC) into a focused
Python coroutine: drive `run_attempt` repeatedly, executing tool calls
between turns, retrying on transient errors, and stopping when the
assistant emits a non-tool stop_reason or hits the iteration cap.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
)
from oxenclaw.pi.models import Model
from oxenclaw.pi.run.attempt import AttemptResult, run_attempt
from oxenclaw.pi.run.error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)
from oxenclaw.pi.run.failover import resolve_next_model, should_failover
from oxenclaw.pi.run.preemptive_compaction import (
    CompactionRoute,
    truncate_tool_results,
)
from oxenclaw.pi.run.preemptive_compaction import (
    decide as decide_compaction,
)
from oxenclaw.pi.run.runtime import RuntimeConfig
from oxenclaw.pi.run.stop_recovery import (
    build_recovery_nudge,
    is_recoverable_empty,
)
from oxenclaw.pi.tool_result_storage import (
    BudgetConfig,
    enforce_turn_budget,
    maybe_persist_tool_result,
)
from oxenclaw.pi.tools import AgentTool, ToolExecutionResult
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.run")


@dataclass
class TurnResult:
    """Outcome of a complete agent turn (one user message → final assistant)."""

    final_message: AssistantMessage
    appended_messages: list[Any] = field(default_factory=list)
    attempts: list[AttemptResult] = field(default_factory=list)
    tool_executions: list[ToolExecutionResult] = field(default_factory=list)
    usage_total: dict[str, int] = field(default_factory=dict)
    stopped_reason: str = "end_turn"


def _accumulate_usage(total: dict[str, int], delta: dict[str, Any] | None) -> None:
    if not delta:
        return
    for key, value in delta.items():
        if isinstance(value, (int, float)):
            total[key] = int(total.get(key, 0) + value)


# Decorrelated jitter: every coroutine that touches `random.uniform`
# shares the global RNG state, so concurrent gateway sessions retrying
# in lockstep produce nearly-identical sleep intervals — a thundering
# herd against the same 429 wall. We keep a per-process monotonic
# counter under a lock and seed a private `Random` instance from
# (counter ^ time_ns) so each retry's jitter is truly decoupled from
# the others. Cheap (~µs) and prevents the herd.
_RETRY_COUNTER = itertools.count()
_RETRY_COUNTER_LOCK = threading.Lock()


def _backoff_delay(attempt: int, *, initial: float, cap: float) -> float:
    base = min(cap, initial * (2 ** max(0, attempt - 1)))
    with _RETRY_COUNTER_LOCK:
        seed = next(_RETRY_COUNTER) ^ time.monotonic_ns()
    rng = random.Random(seed)
    # Full jitter (decorrelated): uniform [base/2, base] keeps ≥half the
    # backoff while still spreading concurrent retries over a window.
    return rng.uniform(base / 2.0, base) if base > 0 else 0.0


def _resolve_retry_delay(
    *,
    retry: int,
    retry_after_seconds: float | None,
    initial: float,
    cap: float,
) -> float:
    """Pick the actual sleep before retrying.

    `retry_after_seconds` (parsed from `Retry-After` / `x-ratelimit-reset-*`)
    wins when present so we don't pound a quota wall — capped at one
    hour to bound damage from broken provider responses. Otherwise
    decorrelated jittered backoff.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return min(float(retry_after_seconds), 3600.0)
    return _backoff_delay(retry, initial=initial, cap=cap)


async def _maybe_rotate_credential(
    config: RuntimeConfig,
    *,
    provider: str,
    api_key: str | None,
    classified: ClassifiedError,
) -> None:
    """Best-effort: ask the AuthKeyPool to cool the active key down.

    The pool isn't always wired up — `config.hook_runner` might be a
    plain runner without the `_auth` adapter, or the pool may not have
    a `report_failure` method.  Any AttributeError / Exception just
    means we have no pool to talk to; the run loop continues with the
    existing retry/failover path."""
    if not provider:
        return
    auth_adapter = getattr(getattr(config, "hook_runner", None), "_auth", None)
    if auth_adapter is None:
        return
    pool = getattr(auth_adapter, "pool", None) or getattr(auth_adapter, "_pool", None)
    if pool is None or not hasattr(pool, "report_failure"):
        return
    # Without a key id we can't pin the failure; the pool's report_failure
    # iterates entries by key_id. Some adapters expose a "current key id"
    # via `auth_adapter.current_key_id(provider)`.
    key_id: str | None = None
    if hasattr(auth_adapter, "current_key_id"):
        try:
            key_id = auth_adapter.current_key_id(provider)
        except Exception:
            key_id = None
    if key_id is None and api_key:
        # Fallback: short-prefix label, matches what the rate-limit
        # tracker stores so observability lines up.
        key_id = api_key[:8]
    if key_id is None:
        return
    try:
        await pool.report_failure(
            provider,
            key_id,
            status=None,
            message=classified.message[:200],
            retry_after_seconds=classified.retry_after_seconds,
        )
    except Exception:
        # Pool failure is never fatal to the run loop.
        logger.debug("auth pool report_failure swallowed", exc_info=True)


def _maybe_record_long_context_tier(config: RuntimeConfig, model: Model, message: str) -> None:
    """Detect Anthropic's "exceeds 200k context tier" signal and flag it.

    The next request will need to hard-cap at 200K tokens before sending
    so the provider doesn't immediately reject it again. We persist a
    session-scoped flag on `config.hook_context.session_flags` (a dict
    PiAgent's hooks layer keeps around) when present; otherwise the
    flag goes on `config.extra_params['force_200k_context']` so the
    next call sees it via SimpleStreamOptions.extra_params.
    """
    msg_lc = (message or "").lower()
    if "long context tier" not in msg_lc and "200k context tier" not in msg_lc:
        return
    flag_set = False
    hook_ctx = getattr(config, "hook_context", None)
    if hook_ctx is not None:
        flags = getattr(hook_ctx, "session_flags", None)
        if isinstance(flags, dict):
            flags["force_200k_context"] = True
            flag_set = True
    # Always also write into extra_params so SimpleStreamOptions sees it
    # even when no hook_context is configured.
    try:
        config.extra_params["force_200k_context"] = True
        flag_set = True
    except Exception:
        pass
    if flag_set:
        logger.warning(
            "anthropic long-context-tier signal: model=%s — next request will be capped at 200K",
            getattr(model, "id", "?"),
        )


async def _execute_tool_safely(tool: AgentTool, request: ToolUseBlock) -> ToolExecutionResult:
    """Run one tool with timing + structured error capture."""
    started = time.monotonic()
    if request.input.get("_parse_error"):
        # The attempt assembler couldn't parse the streamed JSON. Feed the
        # raw payload back so the model self-corrects on the next iteration.
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=(
                "tool error: arguments were not valid JSON. "
                f"Raw payload: {request.input.get('_raw', '')!r}. "
                "Re-emit with a JSON object literal."
            ),
            is_error=True,
            duration_seconds=time.monotonic() - started,
        )
    try:
        out = await tool.execute(request.input)
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=out if isinstance(out, str) else json.dumps(out),
            duration_seconds=time.monotonic() - started,
        )
    except Exception as exc:
        logger.exception("tool %s raised", request.name)
        return ToolExecutionResult(
            id=request.id,
            name=request.name,
            output=f"tool error: {exc}",
            is_error=True,
            duration_seconds=time.monotonic() - started,
        )


async def _run_tools(
    requests: list[ToolUseBlock],
    tools_by_name: dict[str, AgentTool],
    *,
    parallel: bool,
    hook_runner: Any = None,
    hook_context: Any = None,
) -> list[ToolExecutionResult]:
    if not requests:
        return []

    async def _one(req: ToolUseBlock) -> ToolExecutionResult:
        # before_tool_use hook may rewrite args or short-circuit the call.
        effective_input = req.input
        if hook_runner is not None:
            decision = await hook_runner.run_before_tool_use(
                req.name, dict(req.input or {}), hook_context
            )
            if decision is not None:
                if decision.abort:
                    out_text = decision.substitute_output or (f"tool {req.name!r} aborted by hook")
                    result = ToolExecutionResult(
                        id=req.id,
                        name=req.name,
                        output=out_text,
                        is_error=True,
                    )
                    await hook_runner.run_after_tool_use(
                        req.name,
                        effective_input,
                        out_text,
                        True,
                        hook_context,
                    )
                    return result
                if decision.rewrite_args is not None:
                    effective_input = decision.rewrite_args
        # Re-bind the request if args were rewritten so the downstream
        # _execute_tool_safely path sees the new args.
        if effective_input is not req.input:
            req = ToolUseBlock(id=req.id, name=req.name, input=effective_input)
        tool = tools_by_name.get(req.name)
        if tool is None:
            # LLM tool-name drift recovery: ToolRegistry has a
            # canonicalisation + semantic-alias table for common
            # hallucinations like `memory:set_fact`, `remember`,
            # `wiki.create`. Try that path before giving up. Only
            # fires when the live `tools_by_name` mapping was built
            # from a ToolRegistry; otherwise no-op.
            from oxenclaw.agents.tools import (
                _TOOL_NAME_ALIASES as _aliases,
            )
            from oxenclaw.agents.tools import (
                _canonicalise_tool_name as _canon,
            )

            canon = _canon(req.name)
            for live_name, live_tool in tools_by_name.items():
                if _canon(live_name) == canon:
                    tool = live_tool
                    break
            if tool is None:
                aliased = _aliases.get(canon)
                if aliased is not None:
                    tool = tools_by_name.get(aliased)
        if tool is None:
            result = ToolExecutionResult(
                id=req.id,
                name=req.name,
                output=f"tool {req.name!r} is not registered",
                is_error=True,
            )
            if hook_runner is not None:
                await hook_runner.run_after_tool_use(
                    req.name, effective_input, result.output, True, hook_context
                )
            return result
        result = await _execute_tool_safely(tool, req)
        if hook_runner is not None:
            await hook_runner.run_after_tool_use(
                req.name,
                effective_input,
                result.output or "",
                result.is_error,
                hook_context,
            )
        return result

    if parallel:
        return list(await asyncio.gather(*(_one(r) for r in requests)))
    out: list[ToolExecutionResult] = []
    for r in requests:
        out.append(await _one(r))
    return out


async def run_agent_turn(
    *,
    model: Model,
    api: Any,
    system: str | None,
    history: list[Any],
    tools: list[AgentTool],
    config: RuntimeConfig,
    on_event: Any | None = None,
) -> TurnResult:
    """Drive the full agent turn until end_turn / cap / abort.

    `history` is the running message list; the loop appends to a copy and
    returns the new entries via `TurnResult.appended_messages` so callers
    can persist them (or roll back on error).
    """
    tools_by_name: dict[str, AgentTool] = {t.name: t for t in tools}
    working: list[Any] = list(history)
    appended: list[Any] = []
    attempts: list[AttemptResult] = []
    executions: list[ToolExecutionResult] = []
    usage_total: dict[str, int] = {}
    stop_reason = "end_turn"
    consecutive_unknown_tools = 0  # loop-detection counter
    recoveries_used = 0  # stop-reason recovery counter
    # Compress-then-retry self-heal counter (per turn). Context-overflow
    # / payload-too-large failures break to the outer iteration so the
    # preemptive compactor can shrink context before the next attempt.
    # Capped to keep a permanently-broken turn from looping forever.
    compression_self_heals = 0
    max_compression_self_heals = max(1, getattr(config, "max_compression_self_heals", 2))
    # Failover state: chain head is the active model; cursor walks the
    # configured chain when the active model misbehaves.
    failover_chain: list[str] = list(config.failover_chain or [])
    failover_chain_full = [model.id, *failover_chain] if failover_chain else [model.id]
    failover_cursor = 0
    failover_empty_streak = 0
    active_model = model

    for _iteration in range(config.max_tool_iterations):
        if config.abort_event is not None and config.abort_event.is_set():
            # Distinguish a `sessions_yield` (cooperative early stop)
            # from a forced abort. The yield tool sets the abort event
            # AND emits "yielded: ..." text in its result; if any of
            # the tools we just executed match that signature, treat
            # this as a yield rather than an abort.
            yielded_results = [r for r in executions if r.name == "sessions_yield"]
            if yielded_results:
                reason = (yielded_results[-1].output or "yielded").strip()
                yield_msg = AssistantMessage(
                    content=[TextContent(text=reason)],
                    stop_reason="yielded",
                )
                appended.append(yield_msg)
                return TurnResult(
                    final_message=yield_msg,
                    appended_messages=appended,
                    attempts=attempts,
                    tool_executions=executions,
                    usage_total=usage_total,
                    stopped_reason="yielded",
                )
            abort_msg = AssistantMessage(
                content=[TextContent(text="(aborted)")],
                stop_reason="abort",
            )
            return TurnResult(
                final_message=abort_msg,
                appended_messages=appended,
                attempts=attempts,
                tool_executions=executions,
                usage_total=usage_total,
                stopped_reason="abort",
            )

        # Preemptive compaction — check if the assembled prompt fits
        # under the model's context window. If not, either truncate
        # tool_results in-place or hand the run loop's caller a hint
        # to do a full compaction pass next turn.
        if config.preemptive_compaction:
            decision = decide_compaction(
                system=system,
                messages=working,
                context_window=getattr(model, "context_window", 8192) or 8192,
                threshold_ratio=config.compaction_threshold_ratio,
            )
            if decision.route == CompactionRoute.TRUNCATE_TOOL_RESULTS:
                removed = truncate_tool_results(working)
                logger.info(
                    "preemptive compaction: truncated tool_results "
                    "estimated=%d budget=%d removed_chars=%d",
                    decision.estimated_prompt_tokens,
                    decision.prompt_budget_tokens,
                    removed,
                )
            elif decision.route == CompactionRoute.COMPACT_THEN_SEND:
                # Caller-side compaction — the agent's ContextEngine
                # will handle this between turns. Best we can do here
                # is fall through to truncate-tool-results to give the
                # current turn a chance to land.
                removed = truncate_tool_results(working)
                logger.warning(
                    "preemptive compaction: overflow estimated=%d "
                    "budget=%d overflow=%d — emergency tool-result "
                    "truncation removed %d chars (full compact still "
                    "needed in ContextEngine)",
                    decision.estimated_prompt_tokens,
                    decision.prompt_budget_tokens,
                    decision.overflow_tokens,
                    removed,
                )

        # Retry inner loop for transient errors.
        retry = 0
        # Silent-retry budget for mid-stream transport drops — separate
        # from the regular retry budget so a flaky network doesn't burn
        # the user-visible retry budget. We only spend silent retries
        # when the partial stream did NOT emit any user-visible text;
        # once text reached the channel, retrying would duplicate
        # output, so we fall back to the normal retry path.
        silent_retries = 0
        max_silent_retries = max(2, config.max_retries // 2)
        # Sentinel: when set by the classifier (`should_compress`), break
        # the inner retry loop and move to the next outer iteration so
        # preemptive_compaction trims context before the next attempt.
        compress_break = False
        while True:
            result = await run_attempt(
                model=active_model,
                api=api,
                system=system,
                messages=working,
                tools=tools,
                config=config,
                on_event=on_event,
            )
            attempts.append(result)
            if result.error is None:
                break
            # Mid-stream silent retry: when no user-visible text was
            # streamed yet, the user wouldn't see the duplicate, so we
            # can transparently re-issue the same attempt. Bypasses the
            # regular retry budget.
            if (
                result.error.retryable
                and not getattr(result, "text_emitted", False)
                and silent_retries < max_silent_retries
            ):
                silent_retries += 1
                delay = _resolve_retry_delay(
                    retry=silent_retries,
                    retry_after_seconds=getattr(result.error, "retry_after_seconds", None),
                    initial=config.backoff_initial,
                    cap=config.backoff_max,
                )
                logger.info(
                    "mid-stream silent retry %d/%d after %.2fs (no text emitted yet): %s",
                    silent_retries,
                    max_silent_retries,
                    delay,
                    result.error.message,
                )
                await asyncio.sleep(delay)
                continue

            # ─── Classify the failure ──────────────────────────────────
            # The classifier maps the streamed ErrorEvent into a
            # structured recovery hint. From here the loop dispatches:
            # rotate credential, compress + retry, force failover, or
            # plain retry/terminal.
            classified = classify_api_error(error=result.error)

            # Best-effort credential rotation. The auth pool isn't
            # always wired up — when it isn't, this is a no-op.
            if classified.should_rotate_credential:
                await _maybe_rotate_credential(
                    config,
                    provider=active_model.provider,
                    api_key=getattr(api, "api_key", None),
                    classified=classified,
                )

            # Failover decision — checked BEFORE giving up. If the
            # configured chain has another model and the failure looks
            # structural OR the classifier explicitly asked for fallback,
            # swap and retry without burning the retry budget. Single-
            # attempt fast path; the new model gets its own retry budget
            # on the next outer iteration.
            if config.failover_registry is not None and len(failover_chain_full) > 1:
                decision = should_failover(
                    result=result,
                    chain=failover_chain_full,
                    chain_cursor=failover_cursor,
                    empty_streak=failover_empty_streak,
                    empty_streak_threshold=config.failover_empty_streak_threshold,
                )
                if decision.failover or classified.should_fallback:
                    next_model, new_cursor = resolve_next_model(
                        failover_chain_full, failover_cursor, config.failover_registry
                    )
                    if next_model is not None:
                        logger.warning(
                            "failover: %s → %s reason=%s classifier=%s",
                            active_model.id,
                            next_model.id,
                            decision.reason,
                            classified.reason.value,
                        )
                        active_model = next_model
                        failover_cursor = new_cursor
                        failover_empty_streak = 0
                        # Resolve api against the new model — different
                        # provider may need different auth.
                        try:
                            from oxenclaw.pi.auth import resolve_api as _resolve_api

                            new_api = await _resolve_api(active_model, config.hook_runner._auth)  # type: ignore[attr-defined]
                            api = new_api
                        except Exception:
                            # Fall back to the original api object — the
                            # provider stream wrapper may still accept it
                            # if it's a same-family provider.
                            pass
                        continue

            # Compress-then-retry: classifier wants context shrunk before
            # we re-issue. Break to the outer iteration so preemptive
            # compaction runs next round; refund the retry budget so this
            # self-heal doesn't burn the user's retries. Cap per-turn
            # attempts so a permanently-broken request can't loop forever.
            if (
                classified.should_compress
                and classified.reason
                in (
                    FailoverReason.CONTEXT_OVERFLOW,
                    FailoverReason.PAYLOAD_TOO_LARGE,
                )
                and getattr(config, "compress_then_retry", True)
                and compression_self_heals < max_compression_self_heals
            ):
                compression_self_heals += 1
                # Anthropic-specific long-context tier signal: when the
                # provider says we exceeded the 200k tier we set a flag
                # so the agent's session state can hard-cap the next
                # request to 200K before sending. Persisted via
                # `config.hook_context.session_flags` when available.
                _maybe_record_long_context_tier(config, active_model, classified.message)
                logger.warning(
                    "classifier %s → compress-then-retry %d/%d (refunding retry budget): %s",
                    classified.reason.value,
                    compression_self_heals,
                    max_compression_self_heals,
                    classified.message[:200],
                )
                compress_break = True
                break

            # Terminal: non-retryable classification, or out of retries.
            if not classified.retryable or retry >= config.max_retries:
                stop_reason = "error"
                # Even on terminal error, surface what we got so callers can log.
                appended.append(result.message)
                return TurnResult(
                    final_message=result.message,
                    appended_messages=appended,
                    attempts=attempts,
                    tool_executions=executions,
                    usage_total=usage_total,
                    stopped_reason=stop_reason,
                )
            retry += 1
            # Honour `retry_after` from either the classifier (which
            # carries the parsed value) or the raw ErrorEvent.
            ra = classified.retry_after_seconds
            if ra is None:
                ra = getattr(result.error, "retry_after_seconds", None)
            delay = _resolve_retry_delay(
                retry=retry,
                retry_after_seconds=ra,
                initial=config.backoff_initial,
                cap=config.backoff_max,
            )
            logger.warning(
                "transient error (classifier=%s retry %d/%d after %.2fs, retry_after=%s): %s",
                classified.reason.value,
                retry,
                config.max_retries,
                delay,
                ra,
                result.error.message,
            )
            await asyncio.sleep(delay)
        # Compress-then-retry: skip the rest of this outer iteration's
        # tool / message handling and let the next iteration's
        # preemptive_compaction take a swing.
        if compress_break:
            continue

        msg = result.message
        appended.append(msg)
        working.append(msg)
        _accumulate_usage(usage_total, result.usage)

        tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]
        if not tool_uses or msg.stop_reason not in ("tool_use", "tool_calls"):
            # Stop-reason recovery: empty reply or refusal → re-ask once.
            # We don't trigger inside a tool-use chain (those have their
            # own follow-up turn).
            if is_recoverable_empty(msg):
                failover_empty_streak += 1
            else:
                failover_empty_streak = 0
            if recoveries_used < config.stop_reason_recovery_attempts and is_recoverable_empty(msg):
                recoveries_used += 1
                nudge = build_recovery_nudge(msg.stop_reason)
                logger.warning(
                    "stop-reason recovery: stop_reason=%r → re-ask (attempt %d/%d)",
                    msg.stop_reason,
                    recoveries_used,
                    config.stop_reason_recovery_attempts,
                )
                appended.append(nudge)
                working.append(nudge)
                continue
            stop_reason = msg.stop_reason or "end_turn"
            return TurnResult(
                final_message=msg,
                appended_messages=appended,
                attempts=attempts,
                tool_executions=executions,
                usage_total=usage_total,
                stopped_reason=stop_reason,
            )

        results = await _run_tools(
            tool_uses,
            tools_by_name,
            parallel=config.parallel_tools,
            hook_runner=config.hook_runner,
            hook_context=config.hook_context,
        )
        # Layer-2/3 tool-result persistence: when a storage dir is wired,
        # walk the freshly-returned results and spill any oversize outputs
        # to disk before they enter the context. If the aggregate of this
        # turn's results still exceeds the per-turn budget, walk again
        # and spill the largest non-pinned, non-persisted entries.
        if config.tool_result_storage_dir is not None:
            _budget = BudgetConfig()
            _persisted: list[ToolExecutionResult] = []
            for r in results:
                replaced = maybe_persist_tool_result(
                    tool_use_id=r.id,
                    tool_name=r.name,
                    output=r.output or "",
                    config=_budget,
                    storage_dir=config.tool_result_storage_dir,
                )
                if replaced != (r.output or ""):
                    _persisted.append(
                        ToolExecutionResult(
                            id=r.id,
                            name=r.name,
                            output=replaced,
                            is_error=r.is_error,
                            duration_seconds=r.duration_seconds,
                        )
                    )
                else:
                    _persisted.append(r)
            _shadow = [{"id": r.id, "name": r.name, "output": r.output or ""} for r in _persisted]
            enforce_turn_budget(
                _shadow,
                _budget,
                config.tool_result_storage_dir,
            )
            results = [
                ToolExecutionResult(
                    id=r.id,
                    name=r.name,
                    output=sh["output"],
                    is_error=r.is_error,
                    duration_seconds=r.duration_seconds,
                )
                if r.output != sh["output"]
                else r
                for r, sh in zip(_persisted, _shadow, strict=False)
            ]
        executions.extend(results)
        # Loop detection: count this iteration's unknown-tool calls. If
        # the model keeps hammering a non-existent tool, abort the turn
        # so the user sees a structured error instead of an infinite
        # spinner. A single iteration with all-unknown tool calls
        # increments the streak; any iteration with at least one
        # successful (registered) tool resets it.
        unknown_in_iter = sum(
            1 for r in results if r.is_error and "is not registered" in (r.output or "")
        )
        if unknown_in_iter and unknown_in_iter == len(results):
            consecutive_unknown_tools += 1
        else:
            consecutive_unknown_tools = 0
        if consecutive_unknown_tools >= config.unknown_tool_threshold:
            unknown_names = sorted({r.name for r in results if r.is_error})
            abort_text = (
                "(loop-detection abort: model called unknown tool(s) "
                f"{unknown_names!r} {consecutive_unknown_tools} times in a row. "
                "Available tools: "
                f"{sorted(tools_by_name.keys())[:20]}...)"
            )
            logger.warning(
                "loop-detection abort: streak=%d threshold=%d names=%s",
                consecutive_unknown_tools,
                config.unknown_tool_threshold,
                unknown_names,
            )
            abort_msg = AssistantMessage(
                content=[TextContent(text=abort_text)],
                stop_reason="loop_detection",
            )
            tr_msg = ToolResultMessage(
                results=[
                    ToolResultBlock(tool_use_id=r.id, content=r.output, is_error=r.is_error)
                    for r in results
                ]
            )
            appended.append(tr_msg)
            appended.append(abort_msg)
            return TurnResult(
                final_message=abort_msg,
                appended_messages=appended,
                attempts=attempts,
                tool_executions=executions,
                usage_total=usage_total,
                stopped_reason="loop_detection",
            )
        tr_msg = ToolResultMessage(
            results=[
                ToolResultBlock(tool_use_id=r.id, content=r.output, is_error=r.is_error)
                for r in results
            ]
        )
        appended.append(tr_msg)
        working.append(tr_msg)

    # Iteration cap hit.
    cap_msg = AssistantMessage(
        content=[TextContent(text="(stopped: reached max tool iterations without a final answer)")],
        stop_reason="iteration_cap",
    )
    if config.soft_iteration_cap:
        appended.append(cap_msg)
    return TurnResult(
        final_message=cap_msg,
        appended_messages=appended,
        attempts=attempts,
        tool_executions=executions,
        usage_total=usage_total,
        stopped_reason="iteration_cap",
    )


__all__ = ["TurnResult", "run_agent_turn"]
