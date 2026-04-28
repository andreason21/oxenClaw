"""Tests for agents.providers / agents.create / agents.delete RPCs."""

from __future__ import annotations

from oxenclaw.agents import AgentRegistry
from oxenclaw.gateway.agents_methods import register_agents_methods
from oxenclaw.gateway.router import Router


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


# ─── agents.set_model + agents.models ─────────────────────────────────


class _FakePiAgent:
    """Minimal stand-in exposing the surface `agents.set_model` exercises."""

    id = "pi"

    def __init__(self) -> None:
        self._model = type(
            "M", (), {"id": "m1", "provider": "ollama", "aliases": (), "context_window": 4096}
        )()
        self._registry = self  # so getattr(_registry, "list", ...) finds list()
        self._calls: list[str] = []

    def list(self) -> list:
        return [
            type(
                "M", (), {"id": "m1", "provider": "ollama", "aliases": (), "context_window": 4096}
            )(),
            type(
                "M",
                (),
                {
                    "id": "m2",
                    "provider": "anthropic",
                    "aliases": ("claude",),
                    "context_window": 200000,
                },
            )(),
        ]

    def set_model_id(self, model_id: str) -> str:
        if model_id not in {"m1", "m2"}:
            raise KeyError(f"unknown model {model_id!r}")
        self._calls.append(model_id)
        self._model = type(
            "M", (), {"id": model_id, "provider": "ollama", "aliases": (), "context_window": 4096}
        )()
        return model_id


async def test_set_model_swaps_model() -> None:
    router, registry = _setup()
    fake = _FakePiAgent()
    registry.register(fake)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.set_model",
            "params": {"id": "pi", "model": "m2"},
        }
    )
    assert resp.result == {"ok": True, "id": "pi", "model": "m2"}
    assert fake._calls == ["m2"]


async def test_set_model_rejects_unknown_agent() -> None:
    router, _ = _setup()
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.set_model",
            "params": {"id": "ghost", "model": "m1"},
        }
    )
    assert resp.result["ok"] is False
    assert "not registered" in resp.result["error"]


async def test_set_model_rejects_agent_without_setter() -> None:
    """EchoAgent has no model — RPC must return a structured error."""
    router, _ = _setup()
    await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.create",
            "params": {"id": "echoer", "provider": "echo"},
        }
    )
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "agents.set_model",
            "params": {"id": "echoer", "model": "m1"},
        }
    )
    assert resp.result["ok"] is False
    assert "set_model_id" in resp.result["error"]


async def test_set_model_unknown_model_returns_keyerror() -> None:
    router, registry = _setup()
    registry.register(_FakePiAgent())
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "agents.set_model",
            "params": {"id": "pi", "model": "ghost"},
        }
    )
    assert resp.result["ok"] is False
    assert "ghost" in resp.result["error"]


async def test_models_lists_from_first_pi_registry() -> None:
    router, registry = _setup()
    registry.register(_FakePiAgent())
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.models"})
    ids = [m["id"] for m in resp.result]
    assert ids == ["m1", "m2"]
    # aliases passed through
    by_id = {m["id"]: m for m in resp.result}
    assert by_id["m2"]["aliases"] == ["claude"]
    assert by_id["m2"]["context_window"] == 200000


async def test_models_empty_when_no_pi_agent() -> None:
    router, _ = _setup()
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "agents.models"})
    assert resp.result == []
