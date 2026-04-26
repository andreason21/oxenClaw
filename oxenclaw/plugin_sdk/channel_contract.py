"""Channel plugin contract.

Port of openclaw `src/channels/plugins/types.plugin.ts` and `src/plugin-sdk/channel-contract.ts`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class MediaItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["photo", "video", "audio", "voice", "document", "sticker", "animation"]
    source: str
    mime_type: str | None = None
    filename: str | None = None
    caption: str | None = None


class InlineButton(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    callback_data: str | None = None
    url: str | None = None


class ChannelTarget(BaseModel):
    """Identifies a destination on a channel (DM, group, topic thread, …)."""

    model_config = ConfigDict(extra="forbid")

    channel: str
    account_id: str
    chat_id: str
    thread_id: str | None = None


class SendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: ChannelTarget
    text: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    buttons: list[list[InlineButton]] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    edit_message_id: str | None = None


class SendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    timestamp: float
    chunk_ids: list[str] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class ProbeOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str


class ProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    account_id: str
    display_name: str | None = None
    error: str | None = None


class InboundEnvelope(BaseModel):
    """Canonical inbound message delivered to the gateway."""

    model_config = ConfigDict(extra="forbid")

    channel: str
    account_id: str
    target: ChannelTarget
    sender_id: str
    sender_display_name: str | None = None
    text: str | None = None
    media: list[MediaItem] = Field(default_factory=list)
    received_at: float
    raw: dict[str, Any] | None = None


InboundHandler = Callable[[InboundEnvelope], Awaitable[None]]


class MonitorOpts(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    account_id: str
    on_inbound: InboundHandler


@runtime_checkable
class ChannelPlugin(Protocol):
    """Contract every channel plugin must implement.

    Mirrors the TS `ChannelPlugin` interface in openclaw
    `src/channels/plugins/types.plugin.ts`. Runtime-checkable so loader can
    validate duck-typed implementations.
    """

    id: str

    async def send(self, params: SendParams) -> SendResult: ...

    async def monitor(self, opts: MonitorOpts) -> None: ...

    async def probe(self, opts: ProbeOpts) -> ProbeResult: ...
