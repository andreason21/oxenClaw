"""isolation.* gateway RPC tests."""

from __future__ import annotations

from sampyclaw.gateway.isolation_methods import register_isolation_methods
from sampyclaw.gateway.router import Router


async def test_backends_reports_available_and_strongest() -> None:
    router = Router()
    register_isolation_methods(router)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "isolation.backends"}
    )
    assert resp.error is None
    assert "available" in resp.result
    assert "strongest" in resp.result
    assert "inprocess" in resp.result["available"]
    # Strongest must be one of the available ones.
    assert resp.result["strongest"] in resp.result["available"]


async def test_smoke_runs_echo() -> None:
    router = Router()
    register_isolation_methods(router)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "isolation.smoke",
            "params": {"backend": None},
        }
    )
    assert resp.error is None
    assert resp.result["ok"] is True
    assert "isolation-smoke" in resp.result["stdout"]
    assert resp.result["timed_out"] is False


async def test_smoke_pinned_inprocess() -> None:
    router = Router()
    register_isolation_methods(router)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "isolation.smoke",
            "params": {"backend": "inprocess"},
        }
    )
    assert resp.result["backend"] == "inprocess"
    assert resp.result["ok"] is True
