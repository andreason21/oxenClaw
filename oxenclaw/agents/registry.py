"""Agent registry + conversation key derivation.

Registry is a plain dict keyed by agent id. Session keys bind a channel target
to a persistent conversation — openclaw uses `<channel>:<account>:<chat>[:<thread>]`
and we match that shape so session files stay interchangeable.
"""

from __future__ import annotations

from oxenclaw.agents.base import Agent
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        if agent.id in self._agents:
            raise ValueError(f"duplicate agent id: {agent.id}")
        self._agents[agent.id] = agent

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"agent {agent_id!r} not registered")
        return agent

    def ids(self) -> list[str]:
        return sorted(self._agents)


def session_key_for(target: ChannelTarget) -> str:
    """Derive a stable session key from a channel target.

    Matches openclaw's convention: `<channel>:<account>:<chat>[:<thread>]`.
    """
    parts = [target.channel, target.account_id, target.chat_id]
    if target.thread_id:
        parts.append(target.thread_id)
    return ":".join(parts)


def session_key_for_envelope(env: InboundEnvelope) -> str:
    return session_key_for(env.target)
