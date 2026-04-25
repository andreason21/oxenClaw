"""Build a synthetic InboundEnvelope from a CronJob firing.

Sender id is `cron:<job_id>` so downstream code can distinguish scheduler
triggers from real user messages if needed.
"""

from __future__ import annotations

import time

from sampyclaw.cron.models import CronJob
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def build_trigger_envelope(job: CronJob, *, now: float | None = None) -> InboundEnvelope:
    ts = now if now is not None else time.time()
    return InboundEnvelope(
        channel=job.channel,
        account_id=job.account_id,
        target=ChannelTarget(
            channel=job.channel,
            account_id=job.account_id,
            chat_id=job.chat_id,
            thread_id=job.thread_id,
        ),
        sender_id=f"cron:{job.id}",
        text=job.prompt,
        received_at=ts,
    )
