"""Tests for agents.providers / agents.create / agents.delete RPCs."""

from __future__ import annotations

from sampyclaw.agents import AgentRegistry
from sampyclaw.gateway.agents_methods import register_agents_methods
from sampyclaw.gateway.router import Router


def _setup() -> tuple[Router, AgentRegistry]:
    registry = AgentRegistry()
    router = Router()
    register_agents_methods(router, registry)
    return router, registry


async def test_providers_lists_supported() -> None:
    router, _ = _setup()
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.providers"})
    assert "echo" in resp.result
    assert "anthropic" in resp.result


async def test_create_echo_agent() -> None:
    router, registry = _setup()
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.create",
            "params": {"id": "a1", "provider": "echo"},
        }
    )
    assert resp.result == {"created": True, "id": "a1"}
    assert registry.get("a1") is not None


async def test_create_duplicate_returns_error() -> None:
    router, _ = _setup()
    params = {"id": "a1", "provider": "echo"}
    await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.create", "params": params})
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "agents.create", "params": params}
    )
    assert resp.result["created"] is False
    assert "duplicate" in resp.result["error"]


async def test_create_unknown_provider_returns_error() -> None:
    router, _ = _setup()
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.create",
            "params": {"id": "x", "provider": "gpt5"},
        }
    )
    assert resp.result["created"] is False


async def test_delete_removes_registered() -> None:
    router, registry = _setup()
    await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.create",
            "params": {"id": "a1", "provider": "echo"},
        }
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "agents.delete", "params": {"id": "a1"}}
    )
    assert resp.result == {"deleted": True}
    assert registry.get("a1") is None


async def test_delete_missing_returns_false() -> None:
    router, _ = _setup()
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "agents.delete", "params": {"id": "nope"}}
    )
    assert resp.result == {"deleted": False}
