"""Router-level tests for the gateway. Server integration lives in test_gateway_server."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from sampyclaw.gateway import ChatSendParams, ChatSendResult, ErrorCode, Router
from sampyclaw.plugin_sdk.error_runtime import ChannelError, RateLimitedError


@pytest.fixture()
def router() -> Router:
    r = Router()

    @r.method("chat.send", ChatSendParams)
    async def _send(p: ChatSendParams) -> ChatSendResult:
        return ChatSendResult(message_id=f"{p.chat_id}:msg", timestamp=1.0)

    @r.method("ping")
    async def _ping(_: dict) -> dict:  # type: ignore[type-arg]
        return {"pong": True}

    @r.method("explode")
    async def _explode(_: dict) -> None:  # type: ignore[type-arg]
        raise ChannelError("downstream broken")

    @r.method("slow.down")
    async def _slow(_: dict) -> None:  # type: ignore[type-arg]
        raise RateLimitedError("too fast", retry_after=3.0)

    return r


async def test_dispatch_validates_params_and_returns_result(router: Router) -> None:
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.send",
            "params": {
                "channel": "telegram",
                "account_id": "main",
                "chat_id": "42",
                "text": "hi",
            },
        }
    )
    assert resp.error is None
    # `status`/`reason`/`agent_id` are extra fields added in the
    # multi-agent fallback work — the router test only cares that the
    # core fields round-trip.
    assert resp.result is not None
    assert resp.result["message_id"] == "42:msg"
    assert resp.result["timestamp"] == 1.0


async def test_dispatch_unknown_method(router: Router) -> None:
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert resp.error is not None
    assert resp.error.code == ErrorCode.METHOD_NOT_FOUND


async def test_dispatch_invalid_params(router: Router) -> None:
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "chat.send", "params": {"channel": "x"}}
    )
    assert resp.error is not None
    assert resp.error.code == ErrorCode.INVALID_PARAMS


async def test_dispatch_channel_error(router: Router) -> None:
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 4, "method": "explode"})
    assert resp.error is not None
    assert resp.error.code == ErrorCode.CHANNEL_ERROR


async def test_dispatch_rate_limited_carries_retry_after(router: Router) -> None:
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 5, "method": "slow.down"})
    assert resp.error is not None
    assert resp.error.code == ErrorCode.RATE_LIMITED
    assert resp.error.data == {"retry_after": 3.0}


async def test_dispatch_rejects_malformed_request() -> None:
    r = Router()
    resp = await r.dispatch({"not": "a request"})
    assert resp.error is not None
    assert resp.error.code == ErrorCode.INVALID_REQUEST


async def test_dispatch_ignores_missing_params_model(router: Router) -> None:
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert resp.result == {"pong": True}


async def test_duplicate_method_registration_raises() -> None:
    r = Router()

    @r.method("x")
    async def _h(_: dict) -> int:  # type: ignore[type-arg]
        return 1

    with pytest.raises(ValueError):

        @r.method("x")
        async def _h2(_: dict) -> int:  # type: ignore[type-arg]
            return 2


async def test_sync_handler_rejected() -> None:
    r = Router()
    with pytest.raises(TypeError):

        @r.method("bad")
        def _sync(_: dict) -> int:  # type: ignore[type-arg]
            return 1


async def test_basemodel_params_passed_through() -> None:
    class P(BaseModel):
        x: int

    r = Router()

    @r.method("square", P)
    async def _square(p: P) -> int:
        return p.x * p.x

    resp = await r.dispatch({"jsonrpc": "2.0", "id": 7, "method": "square", "params": {"x": 5}})
    assert resp.result == 25
