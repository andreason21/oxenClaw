"""Runtime config — every knob the run loop respects.

Mirrors the union of fields openclaw `pi-embedded-runner/runtime.ts` exposes
plus the per-attempt knobs scattered across `run.ts`. Defaults are tuned
for production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
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

    # Compaction
    compaction_callback: Callable[..., Any] | None = None
    compaction_threshold_ratio: float = 0.85  # of model.context_window

    # Abort
    abort_event: asyncio.Event | None = None

    # Provider-specific extras forwarded to SimpleStreamOptions.extra_params.
    extra_params: dict[str, Any] = field(default_factory=dict)


__all__ = ["RuntimeConfig"]
