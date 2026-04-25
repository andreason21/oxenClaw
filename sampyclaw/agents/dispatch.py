"""Inbound envelope → agent → outbound send dispatcher.

Resolves the target agent using the root config's agent routing, runs the
agent, and hands each outbound `SendParams` to a channel-supplied sender.

Port of the routing/dispatch portion of openclaw `src/agents/dispatch.ts`.
The LLM inference loop is stubbed — agents are expected to implement their
own handling (echo agent for B-phase, real agents in A-phase).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sampyclaw.agents.base import AgentContext
from sampyclaw.agents.registry import AgentRegistry, session_key_for_envelope
from sampyclaw.plugin_sdk.channel_contract import (
    InboundEnvelope,
    SendParams,
    SendResult,
)
from sampyclaw.plugin_sdk.config_schema import RootConfig
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.dispatch")

SendCallable = Callable[[SendParams], Awaitable[SendResult]]


@dataclass
class DispatchOutcome:
    """Structured result of a dispatch call.

    `results` is the list of successfully sent outbound messages.
    `agent_id` is the agent that ran (None on drop).
    `agent_yielded` counts outbound messages the agent emitted —
    importantly, this can be non-zero even when `results` is empty
    (e.g. the dashboard talks via a channel name that has no plugin
    loaded, so `_send` always fails — the agent still ran and saved
    the reply to its conversation history, which the dashboard reads
    via `chat.history`).
    `drop_reason` is set only when no agent ran at all.
    `delivery_warnings` collects per-outbound send failures so callers
    can surface them as warnings without confusing them with a true drop.
    """

    results: list[SendResult] = field(default_factory=list)
    agent_id: str | None = None
    agent_yielded: int = 0
    drop_reason: str | None = None
    delivery_warnings: list[str] = field(default_factory=list)


class Dispatcher:
    def __init__(
        self,
        *,
        agents: AgentRegistry,
        config: RootConfig,
        send: SendCallable,
    ) -> None:
        self._agents = agents
        self._config = config
        self._send = send
        self._sessions: dict[tuple[str, str], AgentContext] = {}

    async def dispatch(self, envelope: InboundEnvelope) -> list[SendResult]:
        """Compatibility wrapper — returns just the send results.

        New callers should use `dispatch_with_outcome` so they can
        surface drop reasons (no agent matched, etc.) to end users.
        """
        outcome = await self.dispatch_with_outcome(envelope)
        return outcome.results

    async def dispatch_with_outcome(
        self, envelope: InboundEnvelope
    ) -> DispatchOutcome:
        agent_id = self._resolve_agent_id(envelope)
        if agent_id is None:
            reason = self._explain_no_agent(envelope)
            logger.info(
                "no agent configured for %s — dropping (%s)",
                envelope.target,
                reason,
            )
            return DispatchOutcome(drop_reason=reason)

        agent = self._agents.get(agent_id)
        if agent is None:
            reason = (
                f"agent {agent_id!r} is referenced by routing but not registered"
            )
            logger.warning("%s — dropping", reason)
            return DispatchOutcome(agent_id=agent_id, drop_reason=reason)

        session_key = session_key_for_envelope(envelope)
        ctx = self._sessions.setdefault(
            (agent_id, session_key),
            AgentContext(agent_id=agent_id, session_key=session_key),
        )
        ctx.history.append(envelope)

        results: list[SendResult] = []
        delivery_warnings: list[str] = []
        agent_yielded = 0
        async for outbound in agent.handle(envelope, ctx):
            agent_yielded += 1
            ctx.history.append(outbound)
            try:
                results.append(await self._send(outbound))
            except UserVisibleError as exc:
                # Routing misconfig (no plugin for target). Log + continue —
                # the agent's own conversation history already captured
                # the reply, so dashboards polling chat.history still see
                # it. We just couldn't bridge the outbound to a wire
                # channel.
                msg = (
                    f"could not deliver to {outbound.target.channel}:"
                    f"{outbound.target.account_id} — {exc}"
                )
                delivery_warnings.append(msg)
                logger.warning("drop outbound: %s", msg)
        return DispatchOutcome(
            results=results,
            agent_id=agent_id,
            agent_yielded=agent_yielded,
            delivery_warnings=delivery_warnings,
        )

    def _resolve_agent_id(self, envelope: InboundEnvelope) -> str | None:
        channel = envelope.channel
        sender = envelope.sender_id

        # 1) Explicit routing in config.yaml takes precedence — operators
        # who declare per-channel agents get exactly the agent they
        # asked for.
        sender_blocked = False
        for agent_id, agent_cfg in self._config.agents.items():
            routing = agent_cfg.channels.get(channel)
            if routing is None:
                continue
            if not routing.allow_from or sender in routing.allow_from:
                return agent_id
            sender_blocked = True

        if sender_blocked:
            # An agent matched the channel but excluded this sender —
            # don't fall back, that would defeat the allow_from filter.
            return None

        # 2) Fallback: when no routing matches AND exactly one agent is
        # registered, treat it as the implicit default. This covers the
        # common single-agent + dashboard case where the operator never
        # bothered to write `agents.<id>.channels.<x>`.
        registered = self._agents.ids()
        if len(registered) == 1:
            return registered[0]
        return None

    def _explain_no_agent(self, envelope: InboundEnvelope) -> str:
        """Human-readable reason for `chat.send` callers / dashboards."""
        registered = self._agents.ids()
        channel = envelope.channel
        if not registered:
            return "no agents are registered with the gateway"
        # Did any agent declare routing for this channel but reject the sender?
        for agent_id, agent_cfg in self._config.agents.items():
            routing = agent_cfg.channels.get(channel)
            if routing is None:
                continue
            if routing.allow_from and envelope.sender_id not in routing.allow_from:
                return (
                    f"agent {agent_id!r} matches channel {channel!r} but the "
                    f"sender {envelope.sender_id!r} is not in allow_from"
                )
        if not any(
            channel in cfg.channels for cfg in self._config.agents.values()
        ):
            return (
                f"no agent declares channel {channel!r} in config.yaml. "
                f"Add `agents.<id>.channels.{channel}: {{}}` or run with a "
                "single agent so the dispatcher can pick it implicitly."
            )
        return f"no agent matched channel {channel!r}"
