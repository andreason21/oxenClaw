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

from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.lanes import BusyPolicy, LaneRegistry
from oxenclaw.agents.registry import AgentRegistry, session_key_for_envelope
from oxenclaw.plugin_sdk.channel_contract import (
    InboundEnvelope,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.config_schema import RootConfig
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError
from oxenclaw.plugin_sdk.runtime_env import get_logger

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
        lanes: LaneRegistry | None = None,
        busy_policy: BusyPolicy | None = None,
    ) -> None:
        self._agents = agents
        self._config = config
        self._send = send
        self._sessions: dict[tuple[str, str], AgentContext] = {}
        # Lane registry — at most one in-flight turn per
        # (agent_id, session_key). Optional global cap configurable
        # at construction. Defaults to "session-only" so concurrent
        # `chat.send` calls on the same session serialise but
        # different sessions still run in parallel.
        if lanes is None:
            lanes = LaneRegistry(busy_policy=busy_policy or "queue")
        elif busy_policy is not None:
            # Allow callers to override the policy on a pre-built
            # registry (used by tests + CLI flag wiring).
            lanes._busy_policy = busy_policy  # type: ignore[attr-defined]
        self._lanes = lanes
        self._busy_policy: BusyPolicy = busy_policy or lanes.busy_policy

    async def dispatch(self, envelope: InboundEnvelope) -> list[SendResult]:
        """Compatibility wrapper — returns just the send results.

        New callers should use `dispatch_with_outcome` so they can
        surface drop reasons (no agent matched, etc.) to end users.
        """
        outcome = await self.dispatch_with_outcome(envelope)
        return outcome.results

    async def dispatch_with_outcome(self, envelope: InboundEnvelope) -> DispatchOutcome:
        # Lane gate: serialise turns per (agent_id, session_key) so
        # two concurrent inbound envelopes for the same chat don't
        # interleave history writes. Resolution happens INSIDE the
        # lane so we don't pay the lock cost on no-agent drops, but
        # we use the resolved key to acquire the right lock.
        agent_id_preview = self._resolve_agent_id(envelope) or "_unrouted"
        session_key_preview = session_key_for_envelope(envelope)
        # Honour the busy policy: when a turn is already in-flight on
        # this lane, we may want to signal an abort (interrupt) or
        # stage the message for mid-stream injection (steer). The
        # actual serialisation still happens via `_lanes.run`.
        lane_state = self._lanes.lane(agent_id_preview, session_key_preview)
        if lane_state.lock.locked():
            policy = self._busy_policy
            if policy == "interrupt":
                self._lanes.signal_abort(agent_id_preview, session_key_preview)
            elif policy == "steer":
                # Best-effort: queue the message text into pending so a
                # `steer(text)`-aware agent can consume it. Falls back
                # to queue-on-lane for agents that don't support it.
                self._lanes.queue_message(agent_id_preview, session_key_preview, envelope.text)
            elif policy == "queue":
                self._lanes.queue_message(agent_id_preview, session_key_preview, envelope.text)
            # `block` falls through silently — the lock provides the
            # blocking behaviour.
        return await self._lanes.run(
            agent_id=agent_id_preview,
            session_key=session_key_preview,
            coro_factory=lambda: self._dispatch_locked(envelope),
        )

    async def _dispatch_locked(self, envelope: InboundEnvelope) -> DispatchOutcome:
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
            reason = f"agent {agent_id!r} is referenced by routing but not registered"
            logger.warning("%s — dropping", reason)
            return DispatchOutcome(agent_id=agent_id, drop_reason=reason)

        session_key = session_key_for_envelope(envelope)
        ctx = self._sessions.setdefault(
            (agent_id, session_key),
            AgentContext(agent_id=agent_id, session_key=session_key),
        )
        ctx.history.append(envelope)
        # Per-turn instrumentation. Without this the gateway log was
        # silent for every successful chat turn — operators couldn't
        # tell from logs alone whether memory recall / tool calls /
        # the model itself fired. The matching "turn done" log lives
        # at the bottom of this function with a duration measurement.
        import time as _time_mod

        _turn_started_at = _time_mod.monotonic()
        text_preview = (envelope.text or "").strip().replace("\n", " ")[:120]
        logger.info(
            "turn start agent=%s session=%s text=%r",
            agent_id,
            session_key,
            text_preview,
        )

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
                # INFO, not WARNING: the agent's reply is already in
                # conversation history (dashboards see it via chat.history),
                # and the structured `delivery_warnings` field carries the
                # detail back to the RPC caller. The dashboard's "fake
                # channel id" routing makes this the expected path, not an
                # operator-actionable warning.
                logger.info("drop outbound: %s", msg)
        elapsed_ms = int((_time_mod.monotonic() - _turn_started_at) * 1000)
        logger.info(
            "turn done agent=%s session=%s yielded=%d delivered=%d warnings=%d elapsed_ms=%d",
            agent_id,
            session_key,
            agent_yielded,
            len(results),
            len(delivery_warnings),
            elapsed_ms,
        )
        return DispatchOutcome(
            results=results,
            agent_id=agent_id,
            agent_yielded=agent_yielded,
            delivery_warnings=delivery_warnings,
        )

    def _resolve_agent_id(self, envelope: InboundEnvelope) -> str | None:
        channel = envelope.channel
        sender = envelope.sender_id

        # 0) Caller-pinned agent wins. Dashboards and RPC clients that
        # already chose an agent (e.g. via the chat target dropdown)
        # don't need channel routing — they tell us directly. If the
        # pinned id is unknown we drop rather than fall through to
        # implicit fallback, since silently routing to a different
        # agent would mask the misconfiguration.
        pinned = envelope.agent_id
        if pinned:
            return pinned if self._agents.get(pinned) is not None else None

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
        if envelope.agent_id and envelope.agent_id not in registered:
            return (
                f"pinned agent {envelope.agent_id!r} is not registered "
                f"(known: {sorted(registered)!r})"
            )
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
        if not any(channel in cfg.channels for cfg in self._config.agents.values()):
            return (
                f"no agent declares channel {channel!r} in config.yaml. "
                f"Add `agents.<id>.channels.{channel}: {{}}` or run with a "
                "single agent so the dispatcher can pick it implicitly."
            )
        return f"no agent matched channel {channel!r}"
