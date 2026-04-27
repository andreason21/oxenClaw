"""JSON-RPC 2.0 message framing + EventFrame + openclaw method payloads.

Port of openclaw `src/gateway/protocol/index.ts`. Method schemas are code-first
Pydantic models (mirror Zod on the TS side). Covers the dashboard / desktop
chat surface plus Slack outbound — extend as new methods land.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from oxenclaw.plugin_sdk.channel_contract import MediaItem

PROTOCOL_VERSION = 1


# ---- JSON-RPC framing ----


class RpcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: int
    message: str
    data: Any | None = None


class RpcResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jsonrpc: Literal["2.0"] = "2.0"
    id: int | str
    result: Any | None = None
    error: RpcError | None = None


# ---- Event frames (server-initiated, outside the RPC request/response pair) ----


class ChatEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["chat"]
    agent_id: str
    session_key: str
    body: dict[str, Any]


class AgentEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["agent"]
    agent_id: str
    body: dict[str, Any]


class CanvasEventFrame(BaseModel):
    """Server -> dashboard canvas push.

    `body` mirrors `oxenclaw.canvas.events.CanvasEvent.to_dict()`.
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal["canvas"]
    agent_id: str
    body: dict[str, Any]


EventBody = Annotated[
    ChatEvent | AgentEvent | CanvasEventFrame,
    Field(discriminator="kind"),
]


class EventFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    body: EventBody


# ---- Method payloads ----


class ChatSendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    chat_id: str = Field(min_length=1)
    text: str
    thread_id: str | None = None
    reply_to_message_id: str | None = None
    # Optional media attachments. Each item's `source` may be a
    # `data:image/...;base64,...` URI or a public `http(s)://` URL —
    # `multimodal/inbound.py:normalize_media_item()` validates and
    # decodes both shapes (10 MiB cap, MIME sniff).
    media: list[MediaItem] = Field(default_factory=list)


class ChatSendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    timestamp: float
    # `status="ok"` when the agent ran and produced at least one outbound;
    # `"dropped"` when no agent was matched or the agent produced no
    # output. `reason` carries a short human-readable explanation in the
    # dropped case so dashboards can render a useful error toast instead
    # of a silent no-op.
    status: str = "ok"
    reason: str | None = None
    agent_id: str | None = None


class AgentsListResult(RootModel[list[str]]):
    pass


class ConfigGetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None


# ---- Error codes (align with JSON-RPC reserved range plus our own) ----


class ErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    UNAUTHORIZED = -32001
    CHANNEL_ERROR = -32010
    RATE_LIMITED = -32011
