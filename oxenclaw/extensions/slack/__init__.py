"""Slack outbound channel — Enterprise Grid friendly, alert-only.

This extension implements the `ChannelPlugin` contract for outbound
delivery only. `monitor()` raises `NotImplementedError` and the
`SlackChannel` sets `outbound_only = True`, which makes the
gateway's monitor supervisor skip spawning a polling task. Inbound
events (Events API webhooks, Socket Mode) are intentionally out of
scope — see docs/SLACK.md for the rationale and a sketch of how
inbound would be added later.

Why outbound-only:
- Internal-network deployments often only need to ping a #alerts
  channel from cron/agent runs, not respond to user messages.
- Inbound brings webhook signing, public ingress, request
  verification, and Slack Connect cross-org complexity that's not
  needed for notifications.
- Enterprise Grid uses the same Web API endpoints — workspace
  bot tokens (`xoxb-`) and org-wide tokens (`xoxe.xoxb-`) both
  authenticate the same `chat.postMessage` call.
"""

from oxenclaw.extensions.slack.channel import SLACK_CHANNEL_ID, SlackChannel
from oxenclaw.extensions.slack.client import SlackApiError, SlackWebClient
from oxenclaw.extensions.slack.token import SlackTokenResolver

__all__ = [
    "SLACK_CHANNEL_ID",
    "SlackApiError",
    "SlackChannel",
    "SlackTokenResolver",
    "SlackWebClient",
]
