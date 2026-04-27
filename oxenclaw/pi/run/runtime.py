"""Runtime config — every knob the run loop respects.

Mirrors the union of fields openclaw `pi-embedded-runner/runtime.ts` exposes
plus the per-attempt knobs scattered across `run.ts`. Defaults are tuned
for production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from oxenclaw.pi.thinking import ThinkingLevel


@dataclass
class RuntimeConfig:
    """Knobs the run loop respects.

    `compaction_callback` is invoked when token usage approaches the model's
    context window — Phase 5 wires the actual compaction. Until then, the
    callback is a no-op and the loop relies on hard truncation in the
    higher-level agent.
    """

    # Per-attempt
    temperature: float = 0.0
    max_tokens: int | None = None
    thinking: ThinkingLevel | str | None = None
    timeout_seconds: float = 300.0
    cache_control_breakpoints: int = 4

    # Retry
    max_retries: int = 3
    backoff_initial: float = 0.5
    backoff_max: float = 8.0

    # Tool loop
    max_tool_iterations: int = 8
    parallel_tools: bool = True
    # If True, on max_tool_iterations the loop appends a synthetic message
    # asking the model to wrap up rather than just terminating.
    soft_iteration_cap: bool = True
    # Loop-detection: if the model calls unknown tools this many times in
    # a row (e.g. gemma4 hammering `web_search` after 0 hits), abort the
    # turn with a structured error so the user sees something instead of
    # a silent stuck loop. Mirrors openclaw `loopDetection.unknownToolThreshold`.
    unknown_tool_threshold: int = 3
    # Stop-reason recovery: when the model returns refusal / safety /
    # sensitive (or end_turn with empty content), retry once with a
    # nudge prefix asking for the answer in plain language. Set to 0
    # to disable. Mirrors openclaw `attempt.stop-reason-recovery.ts`.
    stop_reason_recovery_attempts: int = 1
    # Assistant-failover: when the primary model returns persistent
    # errors (provider_error / overloaded / sustained empty replies),
    # walk this chain. The agent's main model is the implicit head;
    # this list is the failover sequence (e.g.
    # `["claude-3-5-sonnet", "gpt-4o", "llama3.1:8b"]`). Empty list
    # disables failover. Mirrors openclaw assistant-failover.ts.
    failover_chain: list[str] = field(default_factory=list)
    failover_empty_streak_threshold: int = 3
    # Optional model registry handle for failover model resolution.
    # PiAgent injects its registry; standalone callers leave None.
    failover_registry: Any = None

    # Compaction
    compaction_callback: Callable[..., Any] | None = None
    compaction_threshold_ratio: float = 0.85  # of model.context_window
    # Preemptive compaction: estimate token count BEFORE sending to the
    # provider; if over budget, truncate tool_results in place.
    # Cheap heuristic (~3.5 chars/token) — pessimistic by design so we
    # never blow the model's context window. Off → legacy post-turn
    # ContextEngine.compact only.
    preemptive_compaction: bool = True
    # Auxiliary LLM for the structured (LLM-based) summariser pipeline.
    # When provided, PiAgent wires `structured_summarizer_pipeline` in
    # place of the cheap `truncating_summarizer`. Signature:
    #   ``async def auxiliary_llm(prompt: str) -> str``.
    # Leave `None` to keep the byte-for-byte default behaviour.
    auxiliary_llm: Callable[..., Any] | None = None
    # Compress-then-retry self-heal cap per turn. The run loop tries up
    # to this many compress-and-retry cycles when the classifier says
    # the failure was a context-overflow / payload-too-large.
    max_compression_self_heals: int = 2

    # Abort
    abort_event: asyncio.Event | None = None

    # Provider-specific extras forwarded to SimpleStreamOptions.extra_params.
    extra_params: dict[str, Any] = field(default_factory=dict)

    # Optional hook runner for before/after_tool_use, etc. None disables.
    hook_runner: Any = None
    hook_context: Any = None

    # Tool-result persistence (3-layer defense). When set, oversize tool
    # outputs are spilled to ``tool_result_storage_dir/{tool_use_id}.txt``
    # before the ToolResultMessage is built; the in-context content is
    # replaced with a <persisted-output> preview block. None disables —
    # legacy behavior with no on-disk spill.
    tool_result_storage_dir: Path | None = None

    # Self-heal flag consulted by the run loop after error classification.
    # When the classifier flags `should_compress` (context-overflow / payload-
    # too-large), the loop breaks to the outer iteration so preemptive
    # compaction can shrink context before the next attempt. Defaults to True
    # so classifier-driven compression "just works"; set False to disable.
    compress_then_retry: bool = True

    # Optional rate-limit header tracker (oxenclaw.pi.rate_limit_tracker.
    # RateLimitTracker). Provider wrappers call ``.record(...)`` after each
    # successful response so subsequent classification can short-circuit on
    # an exhausted credential. None disables — behaviour unchanged.
    rate_limit_tracker: Any = None

    # Optional shadow-git checkpoint manager
    # (`oxenclaw.security.checkpoint.CheckpointManager`). When wired,
    # callers can request snapshots / restores via the manager directly.
    # The run loop itself does NOT auto-snapshot — the manager is
    # carried alongside config purely so downstream tools can reach it.
    checkpoint_manager: Any = None


__all__ = ["RuntimeConfig"]
