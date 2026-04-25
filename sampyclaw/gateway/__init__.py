"""JSON-RPC over WebSocket gateway. Port of openclaw src/gateway/*."""

from sampyclaw.gateway.protocol import (
    ChatSendParams,
    ChatSendResult,
    ErrorCode,
    EventFrame,
    PROTOCOL_VERSION,
    RpcError,
    RpcRequest,
    RpcResponse,
)
from sampyclaw.gateway.router import Router
from sampyclaw.gateway.server import GatewayServer

__all__ = [
    "ChatSendParams",
    "ChatSendResult",
    "ErrorCode",
    "EventFrame",
    "GatewayServer",
    "PROTOCOL_VERSION",
    "Router",
    "RpcError",
    "RpcRequest",
    "RpcResponse",
]
