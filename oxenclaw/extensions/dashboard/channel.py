"""Dashboard / desktop-client no-op channel plugin."""

from __future__ import annotations

import time
import uuid

from oxenclaw.plugin_sdk.channel_contract import (
    ChannelPlugin,
    MonitorOpts,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)

CHANNEL_ID = "dashboard"


class DashboardChannel(ChannelPlugin):
    """Built-in channel for the web dashboard and native desktop clients.

    The dashboard pushes user messages via the `chat.send` RPC and reads
    agent replies back via `chat.history`. Outbound delivery on this
    channel is therefore a no-op: the agent's reply is already in
    conversation history, and there is no external wire to forward it to.
    Returning a successful `SendResult` keeps the dispatcher happy and
    avoids spurious "could not deliver" warnings.
    """

    id = CHANNEL_ID

    def __init__(self, account_id: str = "main") -> None:
        self._account_id = account_id

    async def send(self, params: SendParams) -> SendResult:
        return SendResult(
            message_id=f"dashboard-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
        )

    async def monitor(self, opts: MonitorOpts) -> None:
        # Dashboard has no separate inbound stream — user messages enter via
        # `chat.send` directly, so monitor never produces envelopes.
        return None

    async def probe(self, opts: ProbeOpts) -> ProbeResult:
        return ProbeResult(ok=True, account_id=opts.account_id, display_name="Dashboard")
