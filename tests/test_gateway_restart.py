"""Tests for the gateway restart RPC + exit code constant."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from oxenclaw.gateway.restart import (
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
    register_restart_method,
)
from oxenclaw.gateway.router import Router


def test_exit_code_is_75() -> None:
    assert GATEWAY_SERVICE_RESTART_EXIT_CODE == 75


@pytest.mark.asyncio
async def test_gateway_restart_rpc_calls_request_restart() -> None:
    router = Router()
    server = MagicMock()
    register_restart_method(router, server)

    response = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "gateway.restart", "params": {}}
    )
    server.request_restart.assert_called_once()
    assert response.result == {"requested": True, "exit_code": 75}


@pytest.mark.asyncio
async def test_gateway_restart_rejects_extra_params() -> None:
    router = Router()
    server = MagicMock()
    register_restart_method(router, server)
    response = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "gateway.restart", "params": {"foo": 1}}
    )
    # Pydantic's `extra="forbid"` returns INVALID_PARAMS.
    assert response.error is not None


def test_server_request_restart_flag() -> None:
    from oxenclaw.gateway.server import GatewayServer
    from oxenclaw.gateway.router import Router

    server = GatewayServer(Router())
    assert server.restart_requested is False
    server.request_restart()
    assert server.restart_requested is True
    # request_restart implies request_shutdown.
    assert server._shutting_down is True
