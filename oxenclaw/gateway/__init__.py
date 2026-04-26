"""JSON-RPC over WebSocket gateway. Port of openclaw src/gateway/*."""

from oxenclaw.gateway.protocol import (
    PROTOCOL_VERSION,
    ChatSendParams,
    ChatSendResult,
    ErrorCode,
    EventFrame,
    RpcError,
    RpcRequest,
    RpcResponse,
)
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.server import GatewayServer

__all__ = [
    "PROTOCOL_VERSION",
    "ChatSendParams",
    "ChatSendResult",
    "ErrorCode",
    "EventFrame",
    "GatewayServer",
    "Router",
    "RpcError",
    "RpcRequest",
    "RpcResponse",
]
