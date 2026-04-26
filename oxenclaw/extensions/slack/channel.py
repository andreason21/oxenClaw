"""Slack channel — outbound-only ChannelPlugin.

`monitor()` raises NotImplementedError; the gateway's monitor
supervisor checks `outbound_only = True` first and skips spawning
a runner, so calling `monitor()` is a defence-in-depth signal for
direct callers, not a runtime path the supervisor takes.
"""

from __future__ import annotations

from oxenclaw.extensions.slack.client import (
    DEFAULT_BASE_URL,
    SlackApiError,
    SlackWebClient,
)
from oxenclaw.extensions.slack.send import send_message_slack
from oxenclaw.plugin_sdk.channel_contract import (
    MonitorOpts,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.security.net.policy import NetPolicy

logger = get_logger("extensions.slack.channel")

SLACK_CHANNEL_ID = "slack"


class SlackChannel:
    """Outbound-only ChannelPlugin for Slack alerts.

    One instance per Slack workspace (or Enterprise Grid org). For a
    multi-workspace Grid with one bot per workspace, register one
    SlackChannel per workspace under distinct `account_id`s.
    """

    id = SLACK_CHANNEL_ID
    # Read by `cli/gateway_cmd.py:_supervise_monitors` to skip spawning
    # a polling task for this binding.
    outbound_only = True

    def __init__(
        self,
        *,
        token: str,
        account_id: str = "main",
        base_url: str = DEFAULT_BASE_URL,
        policy: NetPolicy | None = None,
        client: SlackWebClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("token is required")
        self._account_id = account_id
        self._client = client or SlackWebClient(
            token=token,
            base_url=base_url,
            policy=policy,
        )

    async def send(self, params: SendParams) -> SendResult:
        if params.target.channel != SLACK_CHANNEL_ID:
            raise UserVisibleError(
                f"send called on slack channel with target channel={params.target.channel!r}"
            )
        return await send_message_slack(self._client, params)

    async def monitor(self, opts: MonitorOpts) -> None:
        raise NotImplementedError(
            "slack channel is outbound-only; the gateway monitor supervisor "
            "skips outbound_only plugins. Inbound (Events API / Socket Mode) "
            "is intentionally not supported — see docs/SLACK.md"
        )

    async def probe(self, opts: ProbeOpts) -> ProbeResult:
        """`auth.test` ping — verifies token + workspace identity."""
        try:
            data = await self._client._call("auth.test", {})
        except SlackApiError as exc:
            return ProbeResult(
                ok=False,
                account_id=opts.account_id,
                error=f"{exc.error_code} (http {exc.status})",
            )
        except Exception as exc:
            return ProbeResult(
                ok=False,
                account_id=opts.account_id,
                error=str(exc),
            )
        # auth.test returns {ok, url, team, user, team_id, user_id, ...}
        team = data.get("team") or data.get("team_id") or ""
        user = data.get("user") or ""
        return ProbeResult(
            ok=True,
            account_id=opts.account_id,
            display_name=f"{user}@{team}" if user and team else (user or team or None),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
