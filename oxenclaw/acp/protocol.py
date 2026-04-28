"""ACP JSON-RPC envelope + four foundational verbs.

This is the *minimum* schema surface needed to bring up an ACP
session: `initialize`, `newSession`, `prompt`, `cancel`. The
streaming `session/update` notification envelope is also modelled
because every prompt turn produces zero or more of them.

Methods covered:

  - `initialize`        — capability negotiation, version exchange
  - `session/new`       — mint a new ACP sessionId
  - `session/prompt`    — send a user prompt, receive `session/update`
                          notifications, get a final stop reason
  - `session/cancel`    — abort an in-flight prompt

The TS reference uses `@agentclientprotocol/sdk` for these. We pin
the equivalent pydantic v2 shapes here. `extra="forbid"` is set on
every model so peer drift surfaces as a validation error rather
than a silent field-drop.

The shape mirrors openclaw `src/acp/translator.ts:497-562`
(initialize) + `docs.acp.md:148-212` (full method list).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# Protocol version we advertise. Tracks the openclaw pin
# (`@agentclientprotocol/sdk: 0.19.0`). Bump when the SDK does.
PROTOCOL_VERSION = "0.19.0"


_strict = ConfigDict(extra="forbid")


# --- JSON-RPC envelope ------------------------------------------------------


class _JsonRpcBase(BaseModel):
    model_config = _strict
    jsonrpc: Literal["2.0"] = "2.0"


class JsonRpcRequest(_JsonRpcBase):
    """Peer-initiated request expecting a matching response by id."""

    id: int | str
    method: str
    params: dict[str, Any] | None = None


class JsonRpcNotification(_JsonRpcBase):
    """Fire-and-forget notification (no `id`, no response)."""

    method: str
    params: dict[str, Any] | None = None


class JsonRpcError(BaseModel):
    model_config = _strict
    code: int
    message: str
    data: Any | None = None


class JsonRpcResponse(_JsonRpcBase):
    """Successful response — exactly one of `result` / `error` set."""

    id: int | str
    result: Any | None = None
    error: JsonRpcError | None = None


# --- initialize -------------------------------------------------------------


class InitializeParams(BaseModel):
    model_config = _strict
    protocol_version: str = Field(..., alias="protocolVersion")
    client_info: dict[str, Any] | None = Field(None, alias="clientInfo")
    capabilities: dict[str, Any] | None = None


class InitializeResult(BaseModel):
    model_config = _strict
    protocol_version: str = Field(..., alias="protocolVersion")
    agent_info: dict[str, Any] = Field(..., alias="agentInfo")
    capabilities: dict[str, Any] | None = None


# --- session/new ------------------------------------------------------------


class NewSessionParams(BaseModel):
    model_config = _strict
    cwd: str | None = None
    mcp_servers: list[dict[str, Any]] | None = Field(None, alias="mcpServers")
    # `_meta` is the openclaw escape hatch for sessionKey / sessionLabel
    # / resetSession / requireExisting (docs.acp.md:172-189). Kept open
    # so peers don't trip on extension keys we don't model yet.
    meta: dict[str, Any] | None = Field(None, alias="_meta")


class NewSessionResult(BaseModel):
    model_config = _strict
    session_id: str = Field(..., alias="sessionId")
    meta: dict[str, Any] | None = Field(None, alias="_meta")


# --- session/prompt ---------------------------------------------------------


class PromptContentText(BaseModel):
    model_config = _strict
    type: Literal["text"] = "text"
    text: str


class PromptContentImage(BaseModel):
    model_config = _strict
    type: Literal["image"] = "image"
    mime_type: str = Field(..., alias="mimeType")
    data: str  # base64


class PromptContentResource(BaseModel):
    model_config = _strict
    type: Literal["resource"] = "resource"
    resource: dict[str, Any]


PromptContent = Annotated[
    Union[PromptContentText, PromptContentImage, PromptContentResource],
    Field(discriminator="type"),
]


class PromptParams(BaseModel):
    model_config = _strict
    session_id: str = Field(..., alias="sessionId")
    prompt: list[PromptContent]


class PromptResult(BaseModel):
    """Response to `session/prompt` — final stop reason for the turn."""

    model_config = _strict
    stop_reason: Literal["stop", "cancel", "error"] = Field(..., alias="stopReason")


# --- session/cancel ---------------------------------------------------------


class CancelParams(BaseModel):
    model_config = _strict
    session_id: str = Field(..., alias="sessionId")


# --- session/update notifications ------------------------------------------

# Streaming side-channel emitted while a `session/prompt` is in flight.
# The full list of `update` tag types is in
# `oxenclaw.agents.acp_runtime.OFFICIAL_SESSION_UPDATE_TAGS`. Here we
# carry the envelope and let consumers keep the payload as a free-form
# dict — this commit only pins the four request verbs; the full
# session_update payload schema will arrive when we wire actual
# streaming.


class SessionUpdateParams(BaseModel):
    model_config = _strict
    session_id: str = Field(..., alias="sessionId")
    update: dict[str, Any]


# --- envelope helpers -------------------------------------------------------


def request_envelope(
    *, id: int | str, method: str, params: BaseModel | dict[str, Any] | None
) -> dict[str, Any]:
    """Build a wire-shaped JSON-RPC request envelope."""
    payload = (
        params.model_dump(by_alias=True, exclude_none=True)
        if isinstance(params, BaseModel)
        else (params or None)
    )
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": id, "method": method}
    if payload is not None:
        out["params"] = payload
    return out


def notification_envelope(
    *, method: str, params: BaseModel | dict[str, Any] | None
) -> dict[str, Any]:
    """Build a wire-shaped JSON-RPC notification envelope."""
    payload = (
        params.model_dump(by_alias=True, exclude_none=True)
        if isinstance(params, BaseModel)
        else (params or None)
    )
    out: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if payload is not None:
        out["params"] = payload
    return out


def response_envelope(
    *,
    id: int | str,
    result: BaseModel | dict[str, Any] | None = None,
    error: JsonRpcError | None = None,
) -> dict[str, Any]:
    """Build a wire-shaped JSON-RPC response envelope."""
    if (result is None) == (error is None):
        raise ValueError("response must set exactly one of result / error")
    out: dict[str, Any] = {"jsonrpc": "2.0", "id": id}
    if result is not None:
        out["result"] = (
            result.model_dump(by_alias=True, exclude_none=True)
            if isinstance(result, BaseModel)
            else result
        )
    if error is not None:
        out["error"] = error.model_dump(by_alias=True, exclude_none=True)
    return out


__all__ = [
    "PROTOCOL_VERSION",
    "CancelParams",
    "InitializeParams",
    "InitializeResult",
    "JsonRpcError",
    "JsonRpcNotification",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "NewSessionParams",
    "NewSessionResult",
    "PromptContent",
    "PromptContentImage",
    "PromptContentResource",
    "PromptContentText",
    "PromptParams",
    "PromptResult",
    "SessionUpdateParams",
    "notification_envelope",
    "request_envelope",
    "response_envelope",
]
