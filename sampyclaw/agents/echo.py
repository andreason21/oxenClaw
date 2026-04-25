"""Echo agent — canonical integration-test agent.

Replies with a fixed prefix + the inbound text. Used by B.8 E2E tests to prove
the gateway/channel/agent loop is wired correctly, without requiring an LLM
provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sampyclaw.agents.base import Agent, AgentContext
from sampyclaw.plugin_sdk.channel_contract import InboundEnvelope, SendParams


class EchoAgent(Agent):
    def __init__(self, agent_id: str = "echo", prefix: str = "echo: ") -> None:
        self.id = agent_id
        self._prefix = prefix

    async def handle(
        self, inbound: InboundEnvelope, ctx: AgentContext
    ) -> AsyncIterator[SendParams]:
        text = inbound.text or ""
        if not text:
            return
        yield SendParams(target=inbound.target, text=f"{self._prefix}{text}")
