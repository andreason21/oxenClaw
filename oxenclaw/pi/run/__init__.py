"""Pi run loop — the inference orchestrator.

Modules:
- `runtime`  — RuntimeConfig (knobs the loop respects).
- `attempt`  — one model call: stream events, assemble AssistantMessage,
               execute tools.
- `run`      — multi-attempt loop: stop_reason gating, retry on transient
               errors, parallel tool execution, JSON self-correct, abort.

Public surface:
    from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
"""

from oxenclaw.pi.run.attempt import AttemptResult, run_attempt
from oxenclaw.pi.run.run import TurnResult, run_agent_turn
from oxenclaw.pi.run.runtime import RuntimeConfig

__all__ = [
    "AttemptResult",
    "RuntimeConfig",
    "TurnResult",
    "run_agent_turn",
    "run_attempt",
]
