"""message tool — send a message to a channel from inside the LLM loop.

Mirrors openclaw `message-tool.ts`. Lets the agent push a one-off message
to any (channel, account, chat) target the operator has wired into the
ChannelRouter, without needing the user to copy/paste it.

Best paired with `gated_tool(...)` — sending unsolicited messages is a
sensitive operation; the operator should approve each call.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.channels.router import ChannelRouter
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, SendParams
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError


class _MessageArgs(BaseModel):
    channel: str = Field(..., description="Channel id (e.g. 'slack', 'dashboard').")
    account_id: str = Field(..., description="Account id within the channel.")
    chat_id: str = Field(..., description="Chat / room / user id.")
    text: str = Field(..., description="Message body to send.")
    thread_id: str | None = Field(None, description="Optional thread/topic id.")
    reply_to_message_id: str | None = Field(None, description="Optional reply-to message id.")


def message_tool(router: ChannelRouter) -> Tool:
    async def _h(args: _MessageArgs) -> str:
        target = ChannelTarget(
            channel=args.channel,
            account_id=args.account_id,
            chat_id=args.chat_id,
            thread_id=args.thread_id,
        )
        params = SendParams(
            target=target,
            text=args.text,
            reply_to_message_id=args.reply_to_message_id,
        )
        try:
            result = await router.send(params)
        except UserVisibleError as exc:
            return f"message error: {exc}"
        return (
            f"sent message_id={result.message_id} to "
            f"{args.channel}:{args.account_id}:{args.chat_id}"
        )

    return FunctionTool(
        name="message",
        description=(
            "Send a one-off message to a configured channel target. Use "
            "responsibly — pair with the approval gate when the channel "
            "reaches real users."
        ),
        input_model=_MessageArgs,
        handler=_h,
    )


__all__ = ["message_tool"]
