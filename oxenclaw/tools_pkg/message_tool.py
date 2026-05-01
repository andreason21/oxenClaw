"""message tool — send a message to a channel from inside the LLM loop.

Mirrors openclaw `message-tool.ts`. Lets the agent push a one-off message
to any (channel, account, chat) target the operator has wired into the
ChannelRouter, without needing the user to copy/paste it.

Best paired with `gated_tool(...)` — sending unsolicited messages is a
sensitive operation; the operator should approve each call.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.channels.router import ChannelRouter
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, SendParams
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError
from oxenclaw.tools_pkg._arg_aliases import fold_aliases
from oxenclaw.tools_pkg._desc import hermes_desc


class _MessageArgs(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "text": ("content", "body", "message", "message_text", "msg"),
                "channel": ("channel_id", "channelId", "platform"),
                "account_id": ("account", "accountId"),
                "chat_id": ("chat", "chatId", "conversation_id", "room", "room_id"),
                "thread_id": ("thread", "threadId", "topic_id"),
            },
        )

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
        description=hermes_desc(
            "Send a one-off message to a configured channel target (slack, dashboard, etc.).",
            when_use=[
                "the user explicitly asks you to notify someone",
                "you've completed work the operator wants pushed to a channel",
            ],
            when_skip=[
                "you're answering inline in the current chat (just reply)",
                "the channel/account/chat target is unknown (don't guess)",
            ],
            alternatives={"cron": "schedule a recurring channel message"},
            notes=(
                "External-visible side effect — pair with the approval gate. "
                "Don't invent target ids."
            ),
        ),
        input_model=_MessageArgs,
        handler=_h,
    )


__all__ = ["message_tool"]
