"""Agent contract and shared context.

Port of the minimum surface of openclaw `src/agents/*`. Agents consume an
`InboundEnvelope` and yield zero or more `SendParams` (outbound actions).
Full inference-loop / tool-registry machinery is deferred to phase A.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams


@dataclass
class AgentContext:
    """Per-invocation context passed to an agent.

    `session_key` identifies the conversation thread. `history` is a shared
    transcript the agent may append to; callers decide how it persists.
    """

    agent_id: str
    session_key: str
    history: list[InboundEnvelope | SendParams] = field(default_factory=list)


@runtime_checkable
class Agent(Protocol):
    """Async agent contract. `handle` streams outbound actions as they're decided."""

    id: str

    def handle(self, inbound: InboundEnvelope, ctx: AgentContext) -> AsyncIterator[SendParams]: ...
