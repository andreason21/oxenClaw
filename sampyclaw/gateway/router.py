"""JSON-RPC method dispatcher.

Method handlers register themselves with a schema-validated signature. The
dispatcher is transport-agnostic — the WebSocket server and test harness both
feed it raw dicts and get RpcResponse objects back.
"""

from __future__ import annotations

import inspect
import secrets
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from sampyclaw.approvals.manager import ApprovalAuthError
from sampyclaw.gateway.protocol import ErrorCode, RpcError, RpcRequest, RpcResponse
from sampyclaw.plugin_sdk.error_runtime import ChannelError, RateLimitedError
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("gateway.router")

P = TypeVar("P", bound=BaseModel)
R = TypeVar("R")

Handler = Callable[[Any], Awaitable[Any]]


class Router:
    """Async JSON-RPC method registry + dispatcher."""

    def __init__(self) -> None:
        self._handlers: dict[str, tuple[Handler, type[BaseModel] | None]] = {}

    def method(
        self, name: str, params_model: type[BaseModel] | None = None
    ) -> Callable[[Handler], Handler]:
        """Decorator to register `name` -> async handler. Validates params with Pydantic if provided."""

        def decorator(fn: Handler) -> Handler:
            if name in self._handlers:
                raise ValueError(f"duplicate method registration: {name}")
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"handler for {name} must be async")
            self._handlers[name] = (fn, params_model)
            return fn

        return decorator

    def has(self, name: str) -> bool:
        return name in self._handlers

    async def dispatch(self, raw: dict[str, Any]) -> RpcResponse:
        try:
            req = RpcRequest.model_validate(raw)
        except ValidationError as exc:
            return _err(raw.get("id", 0), ErrorCode.INVALID_REQUEST, str(exc))

        entry = self._handlers.get(req.method)
        if entry is None:
            return _err(req.id, ErrorCode.METHOD_NOT_FOUND, f"unknown method {req.method!r}")

        handler, params_model = entry
        params: Any = req.params
        if params_model is not None:
            try:
                params = params_model.model_validate(req.params)
            except ValidationError as exc:
                return _err(req.id, ErrorCode.INVALID_PARAMS, str(exc))

        try:
            result = await handler(params)
        except RateLimitedError as exc:
            return _err(
                req.id,
                ErrorCode.RATE_LIMITED,
                str(exc),
                data={"retry_after": exc.retry_after},
            )
        except ApprovalAuthError as exc:
            return _err(req.id, ErrorCode.UNAUTHORIZED, str(exc))
        except ChannelError as exc:
            return _err(req.id, ErrorCode.CHANNEL_ERROR, str(exc))
        except Exception:
            # Don't leak internal exception text (paths/SQL/secrets) over the
            # wire. Log the full trace with a correlation id and return a
            # generic message + the id so operators can grep the log.
            corr_id = secrets.token_hex(8)
            logger.exception("unhandled error in method %s [corr=%s]", req.method, corr_id)
            return _err(
                req.id,
                ErrorCode.INTERNAL_ERROR,
                "internal error",
                data={"correlation_id": corr_id},
            )

        if isinstance(result, BaseModel):
            result = result.model_dump()
        return RpcResponse(id=req.id, result=result)


def _err(req_id: Any, code: int, message: str, data: Any = None) -> RpcResponse:
    return RpcResponse(id=req_id, error=RpcError(code=code, message=message, data=data))
