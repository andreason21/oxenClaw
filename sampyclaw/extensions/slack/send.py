"""Slack outbound send — translates `SendParams` to `chat.postMessage`.

`text` is shipped verbatim. `media` items are not uploaded (the
Slack Files API requires a separate multi-step flow); a media-only
SendParams falls back to a one-line summary mentioning the
attachment count so the message isn't silently empty. Inline
buttons (`InlineButton`) are not rendered — Slack's interactive
blocks are expressive but bidirectional; they belong with an
inbound flow we explicitly don't ship here.
"""

from __future__ import annotations

import time

from sampyclaw.extensions.slack.client import SlackApiError, SlackWebClient
from sampyclaw.plugin_sdk.channel_contract import SendParams, SendResult
from sampyclaw.plugin_sdk.error_runtime import (
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)


async def send_message_slack(client: SlackWebClient, params: SendParams) -> SendResult:
    """Outbound for the Slack channel. Returns the Slack message `ts` as our message_id."""
    channel = params.target.chat_id
    if not channel:
        raise UserVisibleError("slack send requires target.chat_id (channel ID like C0123ABCD)")
    body_parts: list[str] = []
    if params.text:
        body_parts.append(params.text)
    if params.media:
        body_parts.append(
            f"_(attached {len(params.media)} item(s); slack outbound channel does not "
            f"upload binaries — see docs/SLACK.md)_"
        )
    text = "\n".join(body_parts) if body_parts else None
    if text is None:
        raise UserVisibleError("slack send requires text or media")
    try:
        result = await client.post_message(
            channel=channel,
            text=text,
            thread_ts=params.target.thread_id,
        )
    except SlackApiError as exc:
        if exc.error_code in ("ratelimited", "rate_limited"):
            raise RateLimitedError(f"slack rate limited: {exc.error_code}") from exc
        raise UserVisibleError(f"slack: {exc.error_code}") from exc
    except Exception as exc:  # connection drops, DNS failures, etc.
        raise NetworkError(f"slack network error: {exc}") from exc
    ts = result.get("ts") or ""
    return SendResult(
        message_id=str(ts),
        timestamp=float(ts) if ts else time.time(),
        raw=result,
    )
