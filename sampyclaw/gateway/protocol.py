"""JSON-RPC 2.0 message framing + EventFrame + openclaw method payloads.

Port of openclaw `src/gateway/protocol/index.ts`. Method schemas are code-first
Pydantic models (mirror Zod on the TS side). Only the subset needed for the
Telegram B-phase flow is defined here; expand in phase A.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel

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


EventBody = Annotated[Union[ChatEvent, AgentEvent], Field(discriminator="kind")]


class EventFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    body: EventBody


# ---- Method payloads (Telegram-flow minimum) ----


class ChatSendParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    account_id: str
    chat_id: str
    text: str
    thread_id: str | None = None
    reply_to_message_id: str | None = None


class ChatSendResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    timestamp: float


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
