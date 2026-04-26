"""JSON-RPC over WebSocket gateway. Port of openclaw src/gateway/*."""

from sampyclaw.gateway.protocol import (
    PROTOCOL_VERSION,
    ChatSendParams,
    ChatSendResult,
    ErrorCode,
    EventFrame,
    RpcError,
    RpcRequest,
    RpcResponse,
)
from sampyclaw.gateway.router import Router
from sampyclaw.gateway.server import GatewayServer

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
