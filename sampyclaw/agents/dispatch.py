"""Inbound envelope → agent → outbound send dispatcher.

Resolves the target agent using the root config's agent routing, runs the
agent, and hands each outbound `SendParams` to a channel-supplied sender.

Port of the routing/dispatch portion of openclaw `src/agents/dispatch.ts`.
The LLM inference loop is stubbed — agents are expected to implement their
own handling (echo agent for B-phase, real agents in A-phase).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

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
        agent_id = self._resolve_agent_id(envelope)
        if agent_id is None:
            logger.info("no agent configured for %s — dropping", envelope.target)
            return []

        agent = self._agents.get(agent_id)
        if agent is None:
            logger.warning("agent %r not registered — dropping", agent_id)
            return []

        session_key = session_key_for_envelope(envelope)
        ctx = self._sessions.setdefault(
            (agent_id, session_key),
            AgentContext(agent_id=agent_id, session_key=session_key),
        )
        ctx.history.append(envelope)

        results: list[SendResult] = []
        async for outbound in agent.handle(envelope, ctx):
            ctx.history.append(outbound)
            try:
                results.append(await self._send(outbound))
            except UserVisibleError as exc:
                # Routing misconfig (no plugin for target). Log + continue —
                # don't blow up the whole turn over one bad outbound.
                logger.warning(
                    "drop outbound %s:%s — %s",
                    outbound.target.channel,
                    outbound.target.account_id,
                    exc,
                )
        return results

    def _resolve_agent_id(self, envelope: InboundEnvelope) -> str | None:
        channel = envelope.channel
        sender = envelope.sender_id

        for agent_id, agent_cfg in self._config.agents.items():
            routing = agent_cfg.channels.get(channel)
            if routing is None:
                continue
            if not routing.allow_from or sender in routing.allow_from:
                return agent_id
        return None
